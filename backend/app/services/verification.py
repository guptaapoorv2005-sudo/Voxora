"""Caller identity verification and PostgreSQL-persisted session management.

Enforces:
- Strict un-normalized four-digit ASCII validation for caller_phone_last4.
- Atomic session creation via PostgreSQL INSERT ... ON CONFLICT DO NOTHING + FOR UPDATE.
- Session-scoped attempt limits and lockout tracking.
- Verification TTL relative to verified_at.
- Privacy-preserving lookup with zero unauthenticated disclosure.
- Safe human reception handoff for ambiguous multiple matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import Appointment, CallerVerificationSession
from app.schemas.dto import AppointmentDTO, VerificationResult
from app.services.booking_errors import (
    InvalidInputError,
    VerificationRequiredError,
)

ASCII_LAST4_PATTERN = re.compile(r"^[0-9]{4}$")


@dataclass(frozen=True)
class VerificationInput:
    """Inputs required to verify a caller against an existing appointment."""

    caller_name: str
    appointment_date: date
    caller_phone_last4: str


def verify_caller_credentials(
    appointment: AppointmentDTO,
    verification: VerificationInput,
    clinic_tz: ZoneInfo,
) -> bool:
    """Validate that caller-provided credentials match an existing appointment.

    Strictly validates raw caller_phone_last4 without silent stripping.
    Leading/trailing whitespace, non-digits, or length != 4 raise InvalidInputError.
    """
    if not isinstance(verification.caller_phone_last4, str) or not ASCII_LAST4_PATTERN.fullmatch(
        verification.caller_phone_last4
    ):
        raise InvalidInputError(
            "caller_phone_last4 must be exactly four ASCII decimal digits (0-9) with no whitespace."
        )

    name_matches = (
        appointment.caller_name.strip().lower() == verification.caller_name.strip().lower()
    )
    phone_matches = appointment.caller_phone.endswith(verification.caller_phone_last4)
    apt_local_date = appointment.start_at.astimezone(clinic_tz).date()
    date_matches = apt_local_date == verification.appointment_date
    return name_matches and phone_matches and date_matches


class CallerVerificationManager:
    """Manages caller verification state backed by PostgreSQL row-level locks."""

    def __init__(
        self,
        max_attempts: int = 3,
        session_ttl_minutes: int = 60,
        verification_ttl_minutes: int = 15,
    ):
        self._max_attempts = max_attempts
        self._session_ttl_minutes = session_ttl_minutes
        self._verification_ttl_minutes = verification_ttl_minutes

    async def get_or_create_session(
        self,
        session: AsyncSession,
        session_id: str,
        clinic_id: int,
    ) -> CallerVerificationSession:
        """Atomically retrieve or create a verification session record under row lock."""
        now_utc = datetime.now(UTC)

        # 1. Atomic insert on conflict do nothing eliminates first-time creation race
        stmt = (
            insert(CallerVerificationSession)
            .values(
                session_id=session_id,
                clinic_id=clinic_id,
                verification_failures=0,
                locked_out=False,
                created_at=now_utc,
                updated_at=now_utc,
            )
            .on_conflict_do_nothing(index_elements=["session_id"])
        )
        await session.execute(stmt)

        # 2. Row lock is guaranteed to find the row
        res = await session.execute(
            select(CallerVerificationSession)
            .where(CallerVerificationSession.session_id == session_id)
            .with_for_update()
        )
        record = res.scalar_one()

        # 3. Check session expiry
        if now_utc >= record.created_at + timedelta(minutes=self._session_ttl_minutes):
            record.verification_failures = 0
            record.locked_out = False
            record.locked_out_at = None
            record.verified_appointment_id = None
            record.verified_at = None
            record.created_at = now_utc
            record.updated_at = now_utc
            await session.flush()

        return record

    async def record_failure(
        self,
        session: AsyncSession,
        session_id: str,
        clinic_id: int,
    ) -> VerificationResult:
        """Increment failure counter under row lock; lock out if threshold reached."""
        rec = await self.get_or_create_session(session, session_id, clinic_id)
        if rec.locked_out:
            return VerificationResult(
                success=False,
                handoff_required=True,
                advisory_message="Session locked out. Transferring to reception.",
            )

        rec.verification_failures += 1
        rec.updated_at = datetime.now(UTC)

        if rec.verification_failures >= self._max_attempts:
            rec.locked_out = True
            rec.locked_out_at = datetime.now(UTC)
            await session.flush()
            return VerificationResult(
                success=False,
                handoff_required=True,
                advisory_message=(
                    "For your security, I cannot access appointment details after multiple "
                    "unsuccessful verification attempts. Let me transfer you to our clinic reception."
                ),
            )

        await session.flush()
        return VerificationResult(
            success=False,
            handoff_required=False,
            advisory_message=(
                "I could not find an appointment matching those details. Please check the name, date, and phone number."
            ),
        )

    async def record_success(
        self,
        session: AsyncSession,
        session_id: str,
        clinic_id: int,
        appointment_id: int,
        appointment: AppointmentDTO,
    ) -> VerificationResult:
        """Record verified appointment status in PostgreSQL under row lock."""
        if appointment_id != appointment.id:
            raise ValueError("Mismatched appointment_id and AppointmentDTO.id in record_success.")

        rec = await self.get_or_create_session(session, session_id, clinic_id)
        if rec.locked_out:
            return VerificationResult(
                success=False,
                handoff_required=True,
                advisory_message="Session locked out. Transferring to reception.",
            )

        rec.verified_appointment_id = appointment_id
        rec.verified_at = datetime.now(UTC)
        rec.updated_at = datetime.now(UTC)
        await session.flush()

        return VerificationResult(success=True, appointment=appointment)

    async def assert_verified(
        self,
        session: AsyncSession,
        session_id: str,
        appointment_id: int,
    ) -> None:
        """Assert that the caller has active verified status for this appointment in this session."""
        res = await session.execute(
            select(CallerVerificationSession).where(
                CallerVerificationSession.session_id == session_id
            )
        )
        rec = res.scalar_one_or_none()
        now_utc = datetime.now(UTC)

        if rec is None or rec.locked_out or rec.verified_appointment_id != appointment_id:
            raise VerificationRequiredError(
                "Caller identity has not been verified for this appointment in this session."
            )

        if rec.verified_at is None or now_utc >= rec.verified_at + timedelta(
            minutes=self._verification_ttl_minutes
        ):
            raise VerificationRequiredError(
                "Caller verification has expired. Please verify your details again."
            )

    async def invalidate_verification(
        self,
        session: AsyncSession,
        session_id: str,
    ) -> None:
        """Clear active verified appointment status upon reschedule, cancellation, or reset."""
        res = await session.execute(
            select(CallerVerificationSession)
            .where(CallerVerificationSession.session_id == session_id)
            .with_for_update()
        )
        rec = res.scalar_one_or_none()
        if rec:
            rec.verified_appointment_id = None
            rec.verified_at = None
            rec.updated_at = datetime.now(UTC)
            await session.flush()


def appointment_model_to_dto(apt: Appointment) -> AppointmentDTO:
    """Convert an attached Appointment ORM model to a frozen AppointmentDTO."""
    return AppointmentDTO(
        id=apt.id,
        clinic_id=apt.clinic_id,
        service_id=apt.service_id,
        service_name=apt.service_name_snapshot,
        caller_name=apt.caller_name,
        caller_phone=apt.caller_phone,
        caller_email=apt.caller_email,
        start_at=apt.start_at,
        end_at=apt.end_at,
        duration_minutes=apt.duration_minutes,
        buffer_minutes=apt.buffer_minutes,
        blocked_until_at=apt.blocked_until_at,
        status=apt.status,
        version=apt.version,
        cancellation_reason=apt.cancellation_reason,
        created_at=apt.created_at,
        updated_at=apt.updated_at,
    )


async def lookup_and_verify_appointment(
    session: AsyncSession,
    verification_manager: CallerVerificationManager,
    session_id: str,
    clinic_id: int,
    verification: VerificationInput,
    clinic_tz: ZoneInfo,
) -> VerificationResult:
    """Find and verify a caller's appointment without disclosing data prior to verification."""
    # 1. Enforce strict raw input validation first
    if not isinstance(verification.caller_phone_last4, str) or not ASCII_LAST4_PATTERN.fullmatch(
        verification.caller_phone_last4
    ):
        raise InvalidInputError(
            "caller_phone_last4 must be exactly four ASCII decimal digits (0-9) with no whitespace."
        )

    # Check lockout under session row lock
    rec = await verification_manager.get_or_create_session(session, session_id, clinic_id)
    if rec.locked_out:
        return VerificationResult(
            success=False,
            handoff_required=True,
            advisory_message=(
                "For your security, I cannot access appointment details after multiple "
                "unsuccessful verification attempts. Let me transfer you to our clinic reception."
            ),
        )

    # 2. Query candidates by phone suffix
    stmt = select(Appointment).where(
        Appointment.clinic_id == clinic_id,
        Appointment.status == "confirmed",
        Appointment.caller_phone.endswith(verification.caller_phone_last4),
    )
    res = await session.execute(stmt)
    candidates = res.scalars().all()

    # 3. Filter candidates by name and date
    matching_appointments: list[AppointmentDTO] = []
    for apt in candidates:
        apt_dto = appointment_model_to_dto(apt)
        if verify_caller_credentials(apt_dto, verification, clinic_tz):
            matching_appointments.append(apt_dto)

    # 4. Handle outcomes
    if len(matching_appointments) == 0:
        return await verification_manager.record_failure(session, session_id, clinic_id)

    elif len(matching_appointments) > 1:
        # Ambiguous match: does not burn failure attempts, but requires human reception handoff
        return VerificationResult(
            success=False,
            handoff_required=True,
            advisory_message=(
                "Multiple appointments match your details. Let me transfer you to reception to assist you directly."
            ),
        )

    else:
        verified_apt = matching_appointments[0]
        return await verification_manager.record_success(
            session=session,
            session_id=session_id,
            clinic_id=clinic_id,
            appointment_id=verified_apt.id,
            appointment=verified_apt,
        )
