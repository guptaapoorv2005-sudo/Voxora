"""Stateful booking engine implementing slot availability, proposals, and atomic mutations.

Enforces:
- The 3-Level Global Lock Hierarchy (Level 1 Proposal -> Level 2 Appointment -> Level 3 Date Advisory).
- Complete transaction ownership by the service layer (clean AsyncSession requirement).
- Committing terminal failure states (expired, rejected, stale) before raising domain errors.
- Single authoritative proposal-to-appointment linkage (confirmed_appointment_id).
- Secure idempotent retry protocol with nonce re-validation.
- Precise PostgreSQL IntegrityError constraint filtering during proposal race recovery.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import Appointment, BookingProposal, ConsentChallenge
from app.db.models.clinic import Service
from app.repositories.booking_repo import (
    get_appointment_by_id,
    get_proposal_by_id,
    get_proposal_by_idempotency_key,
)
from app.schemas.dto import AppointmentDTO, ConsentAuthorization, ProposalDTO
from app.services.availability import (
    acquire_calendar_advisory_locks,
    acquire_reschedule_calendar_advisory_locks,
    check_appointment_conflict,
    normalize_phone,
)
from app.services.booking_errors import (
    AppointmentNotFoundError,
    CancellationWindowError,
    IdempotencyConflictError,
    IdempotencyRaceError,
    InconsistentProposalStateError,
    InvalidAppointmentStatusError,
    InvalidConfirmationTurnError,
    InvalidConsentError,
    ProposalAlreadyUsedError,
    ProposalExpiredError,
    ProposalNotFoundError,
    SessionMismatchError,
    SlotUnavailableError,
    StaleAppointmentVersionError,
)
from app.services.consent import ActionType, verify_consent_signature
from app.services.schedule_validation import lock_schedule_configuration, validate_schedule_slot
from app.services.verification import (
    CallerVerificationManager,
    appointment_model_to_dto,
)

E164_PHONE_PATTERN = re.compile(r"^\+91[6-9][0-9]{9}$")


def proposal_model_to_dto(proposal: BookingProposal) -> ProposalDTO:
    """Convert an attached BookingProposal ORM model to an immutable ProposalDTO."""
    return ProposalDTO(
        id=proposal.id,
        clinic_id=proposal.clinic_id,
        service_id=proposal.service_id,
        service_name=proposal.service_name_snapshot,
        appointment_id=proposal.appointment_id,
        confirmed_appointment_id=proposal.confirmed_appointment_id,
        expected_appointment_version=proposal.expected_appointment_version,
        proposal_type=proposal.proposal_type,
        session_id=proposal.session_id,
        created_turn_id=proposal.created_turn_id,
        duration_minutes=proposal.duration_minutes,
        buffer_minutes=proposal.buffer_minutes,
        caller_name=proposal.caller_name,
        caller_phone=proposal.caller_phone,
        caller_email=proposal.caller_email,
        start_at=proposal.start_at,
        end_at=proposal.end_at,
        blocked_until_at=proposal.blocked_until_at,
        state=proposal.state,
        expires_at=proposal.expires_at,
        idempotency_key=proposal.idempotency_key,
        created_at=proposal.created_at,
        consumed_at=proposal.consumed_at,
    )


def generate_proposal_idempotency_key(
    *,
    clinic_id: int,
    proposal_type: str,
    appointment_id: int | None,
    expected_appointment_version: int | None,
    service_id: int,
    service_name_snapshot: str,
    session_id: str,
    created_turn_id: int,
    caller_name: str,
    caller_phone: str,
    caller_email: str | None,
    duration_minutes: int,
    buffer_minutes: int,
    start_at: datetime,
) -> str:
    """Generate a deterministic 15-field SHA256 idempotency key using canonical JSON serialization."""
    if start_at.tzinfo is None or start_at.utcoffset() is None:
        raise ValueError("start_at must be an explicit, timezone-aware datetime instance.")

    if not E164_PHONE_PATTERN.fullmatch(caller_phone):
        raise ValueError(f"caller_phone must be canonical E.164 (+91...): got {caller_phone}")

    start_at_utc = start_at.astimezone(UTC)
    end_at_utc = start_at_utc + timedelta(minutes=duration_minutes)

    normalized_email = (
        caller_email.strip().lower() if caller_email and caller_email.strip() else None
    )
    normalized_name = caller_name.strip().lower()

    payload = {
        "appointment_id": appointment_id,
        "buffer_minutes": buffer_minutes,
        "caller_email": normalized_email,
        "caller_name": normalized_name,
        "caller_phone": caller_phone,
        "clinic_id": clinic_id,
        "created_turn_id": created_turn_id,
        "duration_minutes": duration_minutes,
        "end_at_utc": end_at_utc.isoformat(),
        "expected_appointment_version": expected_appointment_version,
        "proposal_type": proposal_type,
        "service_id": service_id,
        "service_name_snapshot": service_name_snapshot.strip(),
        "session_id": session_id,
        "start_at_utc": start_at_utc.isoformat(),
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


async def resolve_existing_proposal(
    session: AsyncSession,
    existing: BookingProposal,
    expected: dict[str, Any],
) -> tuple[str, ProposalDTO | str | None]:
    """Execute 15-field immutable comparison and return appropriate action tuple."""
    existing_email = existing.caller_email.strip().lower() if existing.caller_email else None
    if not (
        existing.clinic_id == expected["clinic_id"]
        and existing.proposal_type == expected["proposal_type"]
        and existing.appointment_id == expected["appointment_id"]
        and existing.expected_appointment_version == expected["expected_appointment_version"]
        and existing.service_id == expected["service_id"]
        and existing.service_name_snapshot == expected["service_name_snapshot"]
        and existing.session_id == expected["session_id"]
        and existing.created_turn_id == expected["created_turn_id"]
        and existing.caller_name.strip().lower() == expected["caller_name"].strip().lower()
        and existing.caller_phone == expected["caller_phone"]
        and existing_email == expected["caller_email"]
        and existing.duration_minutes == expected["duration_minutes"]
        and existing.buffer_minutes == expected["buffer_minutes"]
        and existing.start_at.astimezone(UTC) == expected["start_at"].astimezone(UTC)
        and existing.end_at.astimezone(UTC) == expected["end_at"].astimezone(UTC)
    ):
        return ("raise_conflict", "Idempotency key collision with differing proposal parameters.")

    now_utc = datetime.now(UTC)
    if existing.state == "expired":
        return ("raise_expired", "Booking proposal has expired.")
    elif existing.state == "pending" and now_utc >= existing.expires_at:
        existing.state = "expired"
        existing.consumed_at = now_utc
        await session.flush()
        return ("raise_expired", None)
    elif existing.state == "pending" and now_utc < existing.expires_at:
        return ("return_dto", proposal_model_to_dto(existing))
    elif existing.state == "confirmed":
        return ("raise_already_used", "Slot already confirmed.")
    elif existing.state == "rejected":
        return ("raise_slot_unavailable", "Slot previously rejected.")

    return ("return_dto", proposal_model_to_dto(existing))


def _proposal_result(action: tuple[str, ProposalDTO | str | None]) -> ProposalDTO:
    kind, value = action
    if kind == "return_dto" and isinstance(value, ProposalDTO):
        return value
    message = value if isinstance(value, str) else "Booking proposal has expired."
    errors = {"raise_expired": ProposalExpiredError, "raise_already_used": ProposalAlreadyUsedError,
              "raise_slot_unavailable": SlotUnavailableError, "raise_conflict": IdempotencyConflictError}
    if kind in errors:
        raise errors[kind](message)
    raise RuntimeError("Unexpected proposal action")


def _appointment_result(action: tuple[str, AppointmentDTO | str]) -> AppointmentDTO:
    kind, value = action
    if kind == "return_dto" and isinstance(value, AppointmentDTO):
        return value
    if not isinstance(value, str):
        raise RuntimeError("Unexpected appointment action")
    errors = {"raise_expired": ProposalExpiredError, "raise_already_used": ProposalAlreadyUsedError,
              "raise_slot_unavailable": SlotUnavailableError, "raise_invalid_status": InvalidAppointmentStatusError,
              "raise_stale_version": StaleAppointmentVersionError}
    if kind in errors:
        raise errors[kind](value)
    raise RuntimeError("Unexpected appointment action")


class BookingService:
    """Core stateful booking service managing proposals, appointments, and concurrency control."""

    def __init__(self, proposal_ttl_minutes: int = 15):
        self._proposal_ttl_minutes = proposal_ttl_minutes

    async def propose_booking(
        self,
        session: AsyncSession,
        *,
        clinic_id: int,
        service_id: int,
        session_id: str,
        created_turn_id: int,
        caller_name: str,
        caller_phone: str,
        caller_email: str | None,
        start_at: datetime,
    ) -> ProposalDTO:
        """Create or idempotently retrieve an expiring booking proposal."""
        if session.in_transaction():
            raise InvalidRequestError(
                "Booking service requires a clean AsyncSession without an active transaction."
            )

        if start_at.tzinfo is None or start_at.utcoffset() is None:
            raise ValueError("start_at must be an explicit, timezone-aware datetime instance.")

        canonical_phone = normalize_phone(caller_phone)
        start_at_utc = start_at.astimezone(UTC)

        # Lookup service configuration
        svc_res = await session.execute(
            select(Service).where(Service.id == service_id, Service.clinic_id == clinic_id)
        )
        service = svc_res.scalar_one_or_none()
        if service is None:
            raise ValueError(f"Service {service_id} not found for clinic {clinic_id}.")

        duration_minutes = service.duration_minutes
        buffer_minutes = service.buffer_minutes
        end_at_utc = start_at_utc + timedelta(minutes=duration_minutes)
        blocked_until_at_utc = end_at_utc + timedelta(minutes=buffer_minutes)
        service_name_snapshot = service.name

        now_utc = datetime.now(UTC)
        expires_at = now_utc + timedelta(minutes=self._proposal_ttl_minutes)

        idempotency_key = generate_proposal_idempotency_key(
            clinic_id=clinic_id,
            proposal_type="new_booking",
            appointment_id=None,
            expected_appointment_version=None,
            service_id=service_id,
            service_name_snapshot=service_name_snapshot,
            session_id=session_id,
            created_turn_id=created_turn_id,
            caller_name=caller_name,
            caller_phone=canonical_phone,
            caller_email=caller_email,
            duration_minutes=duration_minutes,
            buffer_minutes=buffer_minutes,
            start_at=start_at_utc,
        )

        expected_params = {
            "clinic_id": clinic_id,
            "proposal_type": "new_booking",
            "appointment_id": None,
            "expected_appointment_version": None,
            "service_id": service_id,
            "service_name_snapshot": service_name_snapshot,
            "session_id": session_id,
            "created_turn_id": created_turn_id,
            "caller_name": caller_name,
            "caller_phone": canonical_phone,
            "caller_email": caller_email.strip().lower()
            if caller_email and caller_email.strip()
            else None,
            "duration_minutes": duration_minutes,
            "buffer_minutes": buffer_minutes,
            "start_at": start_at_utc,
            "end_at": end_at_utc,
        }

        action: tuple[str, ProposalDTO | str | None]
        try:
            async with session.begin():
                existing = await get_proposal_by_idempotency_key(session, idempotency_key)
                if existing:
                    action = await resolve_existing_proposal(session, existing, expected_params)
                else:
                    proposal = BookingProposal(
                        id=uuid4(),
                        clinic_id=clinic_id,
                        service_id=service_id,
                        service_name_snapshot=service_name_snapshot,
                        appointment_id=None,
                        confirmed_appointment_id=None,
                        expected_appointment_version=None,
                        proposal_type="new_booking",
                        session_id=session_id,
                        created_turn_id=created_turn_id,
                        duration_minutes=duration_minutes,
                        buffer_minutes=buffer_minutes,
                        caller_name=caller_name.strip(),
                        caller_phone=canonical_phone,
                        caller_email=caller_email.strip().lower()
                        if caller_email and caller_email.strip()
                        else None,
                        start_at=start_at_utc,
                        end_at=end_at_utc,
                        blocked_until_at=blocked_until_at_utc,
                        state="pending",
                        created_at=now_utc,
                        expires_at=expires_at,
                        idempotency_key=idempotency_key,
                        consumed_at=None,
                    )
                    session.add(proposal)
                    await session.flush()
                    dto = proposal_model_to_dto(proposal)
                    action = ("return_dto", dto)
        except IntegrityError as exc:
            # Filter strictly for the expected idempotency unique constraint
            err_str = str(exc).lower()
            if (
                "uq_booking_proposals_idempotency_key" not in err_str
                and "booking_proposals_idempotency_key_key" not in err_str
            ):
                raise

            # Aborted transaction has rolled back. Query winner inside fresh transaction:
            async with session.begin():
                winner = await get_proposal_by_idempotency_key(session, idempotency_key)
                if winner is None:
                    raise IdempotencyRaceError(
                        "Idempotent proposal collided but winner row was not visible."
                    )
                action = await resolve_existing_proposal(session, winner, expected_params)

        return _proposal_result(action)

    async def confirm_booking(
        self,
        session: AsyncSession,
        *,
        proposal_id: UUID,
        voice_session_id: str,
        consent: ConsentAuthorization,
        secret_key: str,
        clinic_tz: tzinfo,
    ) -> AppointmentDTO:
        """Atomically confirm a pending booking proposal under the 3-level lock hierarchy."""
        if session.in_transaction():
            raise InvalidRequestError(
                "Booking service requires a clean AsyncSession without an active transaction."
            )

        now_utc = datetime.now(UTC)
        action: tuple[str, AppointmentDTO | str]

        async with session.begin():
            proposal = await get_proposal_by_id(session, proposal_id)
            if proposal is None:
                raise ProposalNotFoundError(f"Proposal {proposal_id} not found.")

            # Universal session ownership validation
            if proposal.session_id != voice_session_id:
                raise SessionMismatchError("Proposal does not belong to this voice session.")

            # Secure Idempotent Retry Handling
            if proposal.state == "confirmed":
                if proposal.confirmed_appointment_id is None:
                    raise InconsistentProposalStateError(
                        "Confirmed proposal missing confirmed_appointment_id."
                    )
                apt = await get_appointment_by_id(session, proposal.confirmed_appointment_id)
                if apt is None:
                    raise AppointmentNotFoundError("Linked appointment not found.")
                return appointment_model_to_dto(apt)

            # Check proposal expiry (boundary: >= expires_at)
            if proposal.state == "pending" and now_utc >= proposal.expires_at:
                proposal.state = "expired"
                proposal.consumed_at = now_utc
                action = ("raise_expired", "Booking proposal has expired.")
            elif proposal.state != "pending":
                action = ("raise_already_used", f"Proposal is already {proposal.state}.")
            else:
                # Validate cryptographic consent authorization if provided
                if not verify_consent_signature(consent, secret_key, now_utc):
                    raise InvalidConsentError(
                        "Consent authorization signature is invalid or expired."
                    )
                if (
                    consent.session_id != voice_session_id
                    or consent.action_type != ActionType.CONFIRM_BOOKING.value
                    or consent.target_id != str(proposal.id)
                ):
                    raise InvalidConsentError(
                        "Consent authorization does not match this booking proposal."
                    )
                if consent.authorized_turn_id <= proposal.created_turn_id:
                    raise InvalidConfirmationTurnError(
                        "Confirmation turn must be strictly greater than proposal creation turn."
                    )

                # Acquire ordered date advisory locks
                await acquire_calendar_advisory_locks(
                    session,
                    proposal.clinic_id,
                    proposal.start_at,
                    proposal.blocked_until_at,
                    clinic_tz,
                )

                # Recheck availability under lock
                await lock_schedule_configuration(session, proposal.clinic_id)
                try:
                    await validate_schedule_slot(
                        session, clinic_id=proposal.clinic_id, service_id=proposal.service_id,
                        start_at=proposal.start_at, duration_minutes=proposal.duration_minutes,
                        buffer_minutes=proposal.buffer_minutes, service_name=proposal.service_name_snapshot,
                        now=now_utc,
                    )
                except SlotUnavailableError:
                    has_conflict = True
                else:
                    has_conflict = await check_appointment_conflict(
                        session,
                        clinic_id=proposal.clinic_id,
                        start_at=proposal.start_at,
                        duration_minutes=proposal.duration_minutes,
                        buffer_minutes=proposal.buffer_minutes,
                    )

                if has_conflict:
                    proposal.state = "rejected"
                    proposal.consumed_at = now_utc
                    action = ("raise_slot_unavailable", "Requested slot is no longer available.")
                else:
                    # Insert appointment using exact snapshot
                    apt = Appointment(
                        clinic_id=proposal.clinic_id,
                        service_id=proposal.service_id,
                        service_name_snapshot=proposal.service_name_snapshot,
                        caller_name=proposal.caller_name,
                        caller_phone=proposal.caller_phone,
                        caller_email=proposal.caller_email,
                        start_at=proposal.start_at,
                        end_at=proposal.end_at,
                        duration_minutes=proposal.duration_minutes,
                        buffer_minutes=proposal.buffer_minutes,
                        blocked_until_at=proposal.blocked_until_at,
                        status="confirmed",
                        version=1,
                        created_at=now_utc,
                        updated_at=now_utc,
                    )
                    session.add(apt)
                    await session.flush()

                    # Update challenge to consumed if present
                    if proposal.challenge_id:
                        res_ch = await session.execute(
                            select(ConsentChallenge).where(
                                ConsentChallenge.challenge_id == proposal.challenge_id
                            )
                        )
                        ch = res_ch.scalar_one_or_none()
                        if ch:
                            ch.status = "consumed"
                            ch.consumed_at = now_utc

                    # Update proposal state and linkage
                    proposal.state = "confirmed"
                    proposal.consumed_at = now_utc
                    proposal.confirmed_appointment_id = apt.id
                    await session.flush()

                    dto = appointment_model_to_dto(apt)
                    action = ("return_dto", dto)

        return _appointment_result(action)

    async def propose_reschedule(
        self,
        session: AsyncSession,
        verification_manager: CallerVerificationManager,
        *,
        appointment_id: int,
        clinic_id: int,
        service_id: int,
        session_id: str,
        created_turn_id: int,
        start_at: datetime,
    ) -> ProposalDTO:
        """Create or idempotently retrieve a reschedule proposal for a verified appointment."""
        if session.in_transaction():
            raise InvalidRequestError(
                "Booking service requires a clean AsyncSession without an active transaction."
            )

        if start_at.tzinfo is None or start_at.utcoffset() is None:
            raise ValueError("start_at must be an explicit, timezone-aware datetime instance.")

        start_at_utc = start_at.astimezone(UTC)

        # Enforce caller verification before proceeding
        await verification_manager.assert_verified(session, session_id, appointment_id)

        # Retrieve target appointment and service
        apt = await get_appointment_by_id(session, appointment_id)
        if apt is None:
            raise AppointmentNotFoundError(f"Appointment {appointment_id} not found.")
        if apt.status != "confirmed":
            raise InvalidAppointmentStatusError(
                "Cannot reschedule an appointment that is not confirmed."
            )

        svc_res = await session.execute(
            select(Service).where(Service.id == service_id, Service.clinic_id == clinic_id)
        )
        service = svc_res.scalar_one_or_none()
        if service is None:
            raise ValueError(f"Service {service_id} not found for clinic {clinic_id}.")

        duration_minutes = service.duration_minutes
        buffer_minutes = service.buffer_minutes
        end_at_utc = start_at_utc + timedelta(minutes=duration_minutes)
        blocked_until_at_utc = end_at_utc + timedelta(minutes=buffer_minutes)
        service_name_snapshot = service.name

        now_utc = datetime.now(UTC)
        expires_at = now_utc + timedelta(minutes=self._proposal_ttl_minutes)

        idempotency_key = generate_proposal_idempotency_key(
            clinic_id=clinic_id,
            proposal_type="reschedule",
            appointment_id=apt.id,
            expected_appointment_version=apt.version,
            service_id=service_id,
            service_name_snapshot=service_name_snapshot,
            session_id=session_id,
            created_turn_id=created_turn_id,
            caller_name=apt.caller_name,
            caller_phone=apt.caller_phone,
            caller_email=apt.caller_email,
            duration_minutes=duration_minutes,
            buffer_minutes=buffer_minutes,
            start_at=start_at_utc,
        )

        expected_params = {
            "clinic_id": clinic_id,
            "proposal_type": "reschedule",
            "appointment_id": apt.id,
            "expected_appointment_version": apt.version,
            "service_id": service_id,
            "service_name_snapshot": service_name_snapshot,
            "session_id": session_id,
            "created_turn_id": created_turn_id,
            "caller_name": apt.caller_name,
            "caller_phone": apt.caller_phone,
            "caller_email": apt.caller_email.strip().lower()
            if apt.caller_email and apt.caller_email.strip()
            else None,
            "duration_minutes": duration_minutes,
            "buffer_minutes": buffer_minutes,
            "start_at": start_at_utc,
            "end_at": end_at_utc,
        }

        action: tuple[str, ProposalDTO | str | None]
        try:
            async with session.begin():
                existing = await get_proposal_by_idempotency_key(session, idempotency_key)
                if existing:
                    action = await resolve_existing_proposal(session, existing, expected_params)
                else:
                    proposal = BookingProposal(
                        id=uuid4(),
                        clinic_id=clinic_id,
                        service_id=service_id,
                        service_name_snapshot=service_name_snapshot,
                        appointment_id=apt.id,
                        confirmed_appointment_id=None,
                        expected_appointment_version=apt.version,
                        proposal_type="reschedule",
                        session_id=session_id,
                        created_turn_id=created_turn_id,
                        duration_minutes=duration_minutes,
                        buffer_minutes=buffer_minutes,
                        caller_name=apt.caller_name,
                        caller_phone=apt.caller_phone,
                        caller_email=apt.caller_email,
                        start_at=start_at_utc,
                        end_at=end_at_utc,
                        blocked_until_at=blocked_until_at_utc,
                        state="pending",
                        created_at=now_utc,
                        expires_at=expires_at,
                        idempotency_key=idempotency_key,
                        consumed_at=None,
                    )
                    session.add(proposal)
                    await session.flush()
                    dto = proposal_model_to_dto(proposal)
                    action = ("return_dto", dto)
        except IntegrityError as exc:
            err_str = str(exc).lower()
            if (
                "uq_booking_proposals_idempotency_key" not in err_str
                and "booking_proposals_idempotency_key_key" not in err_str
            ):
                raise

            async with session.begin():
                winner = await get_proposal_by_idempotency_key(session, idempotency_key)
                if winner is None:
                    raise IdempotencyRaceError(
                        "Idempotent reschedule collided but winner row was not visible."
                    )
                action = await resolve_existing_proposal(session, winner, expected_params)

        return _proposal_result(action)

    async def confirm_reschedule(
        self,
        session: AsyncSession,
        verification_manager: CallerVerificationManager,
        *,
        proposal_id: UUID,
        voice_session_id: str,
        consent: ConsentAuthorization,
        secret_key: str,
        clinic_tz: tzinfo,
    ) -> AppointmentDTO:
        """Confirm a reschedule proposal under the full 3-level lock hierarchy."""
        if session.in_transaction():
            raise InvalidRequestError(
                "Booking service requires a clean AsyncSession without an active transaction."
            )

        now_utc = datetime.now(UTC)
        action: tuple[str, AppointmentDTO | str]

        async with session.begin():
            proposal = await get_proposal_by_id(session, proposal_id)
            if proposal is None:
                raise ProposalNotFoundError(f"Proposal {proposal_id} not found.")

            # Universal session ownership validation
            if proposal.session_id != voice_session_id:
                raise SessionMismatchError("Proposal does not belong to this voice session.")

            # Secure Idempotent Retry Handling
            if proposal.state == "confirmed":
                if proposal.confirmed_appointment_id is None:
                    raise InconsistentProposalStateError(
                        "Confirmed proposal missing confirmed_appointment_id."
                    )
                apt = await get_appointment_by_id(session, proposal.confirmed_appointment_id)
                if apt is None:
                    raise AppointmentNotFoundError("Linked appointment not found.")
                return appointment_model_to_dto(apt)

            # Check proposal expiry (boundary: >= expires_at)
            if proposal.state == "pending" and now_utc >= proposal.expires_at:
                proposal.state = "expired"
                proposal.consumed_at = now_utc
                action = ("raise_expired", "Reschedule proposal has expired.")
            elif proposal.state != "pending":
                action = ("raise_already_used", f"Proposal is already {proposal.state}.")
            else:
                # Validate cryptographic consent authorization
                if not verify_consent_signature(consent, secret_key, now_utc):
                    raise InvalidConsentError(
                        "Consent authorization signature is invalid or expired."
                    )
                if (
                    consent.session_id != voice_session_id
                    or consent.action_type != ActionType.CONFIRM_RESCHEDULE.value
                    or consent.target_id != str(proposal.id)
                ):
                    raise InvalidConsentError(
                        "Consent authorization does not match this reschedule proposal."
                    )
                if consent.authorized_turn_id <= proposal.created_turn_id:
                    raise InvalidConfirmationTurnError(
                        "Confirmation turn must be strictly greater than proposal creation turn."
                    )
                if consent.expected_target_version != proposal.expected_appointment_version:
                    raise InvalidConsentError(
                        "Consent expected version does not match proposal snapshot."
                    )

                if proposal.appointment_id is None:
                    raise InconsistentProposalStateError(
                        "Reschedule proposal missing appointment_id."
                    )
                apt = await get_appointment_by_id(session, proposal.appointment_id)
                if apt is None:
                    raise AppointmentNotFoundError(
                        f"Appointment {proposal.appointment_id} not found."
                    )

                if apt.status != "confirmed":
                    proposal.state = "rejected"
                    proposal.consumed_at = now_utc
                    action = (
                        "raise_invalid_status",
                        "Cannot reschedule an appointment that is not confirmed.",
                    )
                elif apt.version != proposal.expected_appointment_version:
                    proposal.state = "rejected"
                    proposal.consumed_at = now_utc
                    action = (
                        "raise_stale_version",
                        "Appointment was modified after this reschedule proposal was created.",
                    )
                else:
                    # Advisory locks for union of old and new dates in ascending order
                    await acquire_reschedule_calendar_advisory_locks(
                        session,
                        proposal.clinic_id,
                        apt.start_at,
                        apt.blocked_until_at,
                        proposal.start_at,
                        proposal.blocked_until_at,
                        clinic_tz,
                    )

                    # Recheck availability for replacement slot (excluding self)
                    await lock_schedule_configuration(session, proposal.clinic_id)
                    try:
                        await validate_schedule_slot(
                            session, clinic_id=proposal.clinic_id, service_id=proposal.service_id,
                            start_at=proposal.start_at, duration_minutes=proposal.duration_minutes,
                            buffer_minutes=proposal.buffer_minutes, service_name=proposal.service_name_snapshot,
                            now=now_utc,
                        )
                    except SlotUnavailableError:
                        has_conflict = True
                    else:
                        has_conflict = await check_appointment_conflict(
                            session,
                            clinic_id=proposal.clinic_id,
                            start_at=proposal.start_at,
                            duration_minutes=proposal.duration_minutes,
                            buffer_minutes=proposal.buffer_minutes,
                            exclude_appointment_id=apt.id,
                        )

                    if has_conflict:
                        proposal.state = "rejected"
                        proposal.consumed_at = now_utc
                        action = (
                            "raise_slot_unavailable",
                            "Replacement slot is no longer available.",
                        )
                    else:
                        # Update appointment in place: service, snapshot, duration, buffer, timestamps, version
                        apt.service_id = proposal.service_id
                        apt.service_name_snapshot = proposal.service_name_snapshot
                        apt.start_at = proposal.start_at
                        apt.end_at = proposal.end_at
                        apt.duration_minutes = proposal.duration_minutes
                        apt.buffer_minutes = proposal.buffer_minutes
                        apt.blocked_until_at = proposal.blocked_until_at
                        apt.version = apt.version + 1
                        apt.updated_at = now_utc

                        # Update challenge to consumed if present
                        if proposal.challenge_id:
                            res_ch = await session.execute(
                                select(ConsentChallenge).where(
                                    ConsentChallenge.challenge_id == proposal.challenge_id
                                )
                            )
                            ch = res_ch.scalar_one_or_none()
                            if ch:
                                ch.status = "consumed"
                                ch.consumed_at = now_utc

                        # Update proposal state and linkage
                        proposal.state = "confirmed"
                        proposal.consumed_at = now_utc
                        proposal.confirmed_appointment_id = apt.id

                        # Invalidate active verification session state upon successful reschedule
                        await verification_manager.invalidate_verification(
                            session, voice_session_id
                        )

                        await session.flush()
                        dto = appointment_model_to_dto(apt)
                        action = ("return_dto", dto)
                        dto = appointment_model_to_dto(apt)
                        action = ("return_dto", dto)

        return _appointment_result(action)

    async def cancel_appointment(
        self,
        session: AsyncSession,
        verification_manager: CallerVerificationManager,
        *,
        appointment_id: int,
        voice_session_id: str,
        consent: ConsentAuthorization,
        secret_key: str,
        reason: str | None,
        clinic_tz: tzinfo,
    ) -> AppointmentDTO:
        """Cancel an existing confirmed appointment under Level 2 & Level 3 locks."""
        if session.in_transaction():
            raise InvalidRequestError(
                "Booking service requires a clean AsyncSession without an active transaction."
            )

        now_utc = datetime.now(UTC)

        # Enforce caller verification before proceeding
        await verification_manager.assert_verified(session, voice_session_id, appointment_id)

        async with session.begin():
            # Validate cryptographic consent signature
            if not verify_consent_signature(consent, secret_key, now_utc):
                raise InvalidConsentError("Consent authorization signature is invalid or expired.")
            if (
                consent.session_id != voice_session_id
                or consent.action_type != ActionType.CANCEL_APPOINTMENT.value
                or consent.target_id != str(appointment_id)
            ):
                raise InvalidConsentError(
                    "Consent authorization does not match this appointment cancellation."
                )

            apt = await get_appointment_by_id(session, appointment_id)
            if apt is None:
                raise AppointmentNotFoundError(f"Appointment {appointment_id} not found.")

            if apt.status != "confirmed":
                raise InvalidAppointmentStatusError(
                    "Cannot cancel an appointment that is not confirmed."
                )

            if consent.expected_target_version != apt.version:
                raise StaleAppointmentVersionError(
                    "Appointment was modified after cancellation consent was granted. Please review the updated appointment."
                )

            # Check cancellation policy window
            svc_res = await session.execute(select(Service).where(Service.id == apt.service_id))
            service = svc_res.scalar_one_or_none()
            if service and service.cancellation_window_hours > 0:
                cutoff = now_utc + timedelta(hours=service.cancellation_window_hours)
                if apt.start_at < cutoff:
                    raise CancellationWindowError(
                        f"Cannot cancel within {service.cancellation_window_hours} hours of appointment start time."
                    )

            # Acquire date advisory locks
            await acquire_calendar_advisory_locks(
                session, apt.clinic_id, apt.start_at, apt.blocked_until_at, clinic_tz
            )

            # Update challenge to consumed if present
            if consent.challenge_id:
                try:
                    ch_uuid = UUID(consent.challenge_id)
                    res_ch = await session.execute(
                        select(ConsentChallenge).where(ConsentChallenge.challenge_id == ch_uuid)
                    )
                    ch = res_ch.scalar_one_or_none()
                    if ch:
                        ch.status = "consumed"
                        ch.consumed_at = now_utc
                except (ValueError, TypeError):
                    pass

            # Update appointment status in place
            apt.status = "cancelled"
            apt.version = apt.version + 1
            apt.cancellation_reason = reason.strip() if reason and reason.strip() else None
            apt.updated_at = now_utc

            # Invalidate active verification session state upon cancellation
            await verification_manager.invalidate_verification(session, voice_session_id)

            await session.flush()
            dto = appointment_model_to_dto(apt)

        return dto
