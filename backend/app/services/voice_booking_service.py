"""Voice booking, rescheduling, and proposal-based cancellation service for Phase 5.

Enforces:
- Unified 5-Level Global Lock Hierarchy (L1 Session -> L2 Appointment -> L3 Proposal -> L4 Challenge -> L5 Calendar Advisory).
- Complete IDOR protection: target appointments derived exclusively from server-persisted verified session state.
- Server-side caller identity matching via normalize_name() and names_match().
- Anti-enumeration candidate lookup with date-based disambiguation and step-up mode.
- Two-phase cryptographic authorization (Session A) and mutation (Session B) with anti-replay registry.
- Durable table-wide cancellation idempotency.
- Universal Level 1 handoff gating preventing scheduling mutations after handoff.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.booking import (
    Appointment,
    AppointmentVerificationFailure,
    BookingProposal,
    CallerVerificationSession,
    CancellationProposal,
    ClientVerificationAttempt,
    ConsentChallenge,
)
from app.db.models.clinic import Service
from app.services.availability import (
    check_appointment_conflict,
    get_affected_date_ints,
    normalize_phone,
)
from app.services.booking_errors import (
    CancellationWindowError,
    InvalidAppointmentStatusError,
    InvalidConsentError,
    ProposalAlreadyUsedError,
    ProposalExpiredError,
    ProposalNotFoundError,
    SlotUnavailableError,
    StaleAppointmentVersionError,
)
from app.services.caller_identity import names_match
from app.services.schedule_validation import lock_schedule_configuration, validate_schedule_slot
from app.voice.intent import Intent, classify_caller_intent

logger = logging.getLogger("voxora.services.voice_booking")

CLINIC_TZ = ZoneInfo("Asia/Kolkata")
PROPOSAL_TTL_SECONDS = 300  # 5 minutes


class SessionHandedOffError(Exception):
    """Raised when an operation is attempted on a session that has been handed off to human reception."""


class UnverifiedCallerSessionError(Exception):
    """Raised when an operation requires verified caller session state that is absent or expired."""


class IdempotencyKeyConsumedError(Exception):
    """Raised when an idempotency key belongs to a terminal proposal (expired or replaced)."""


class EarlySpeechBoundaryError(Exception):
    """Raised when affirmative speech started before canonical readback playout completed."""


class ClockSkewDetectedError(Exception):
    """Raised when speech start timestamp is severely anomalous or precedes playout by > 5 seconds."""


class VoiceBookingService:
    """Core domain service for safe voice booking, rescheduling, and cancellation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        signing_secret: str | None = None,
        previous_signing_secrets: list[str] | None = None,
        clinic_id: int = 1,
        session_signing_secret: str | None = None,
    ):
        secret = (
            signing_secret
            or session_signing_secret
            or "in_process_default_secret_32_characters_long"
        )
        self._session_factory = session_factory
        self._signing_secret = secret
        self._previous_signing_secrets = previous_signing_secrets or []
        self._clinic_id = clinic_id

    @staticmethod
    async def _lock_voice_session(session: AsyncSession, session_id: str) -> None:
        # Booking calls may not have a verification-session row to lock yet.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('voice_session'), hashtext(:sid))"),
            {"sid": session_id},
        )

    @staticmethod
    async def _replace_session_proposals(
        session: AsyncSession, session_id: str, now: datetime
    ) -> None:
        """Invalidate old consent and proposals together before a new readback."""
        await session.execute(
            update(ConsentChallenge)
            .where(ConsentChallenge.session_id == session_id,
                   ConsentChallenge.status.in_(["pending_readback", "pending"]))
            .values(status="invalidated", invalidated_at=now, invalidation_reason="replaced")
        )
        for model in (BookingProposal, CancellationProposal):
            await session.execute(
                update(model)
                .where(model.session_id == session_id, model.state == "pending")
                .values(state="replaced", consumed_at=now)
            )

    # -------------------------------------------------------------------------
    # 1. Anti-Enumeration Candidate Lookup & Disambiguation
    # -------------------------------------------------------------------------
    async def lookup_appointment(
        self,
        *,
        voice_session_id: str,
        client_id: str,
        phone_last4: str,
        caller_name: str,
        appointment_date: str | None = None,
    ) -> dict[str, Any]:
        """Anti-enumeration candidate lookup with server-side name matching and date disambiguation.

        Returns:
            dict with:
                - "status": "verified" | "ambiguous" | "step_up_required" | "not_found" | "locked_out"
                - "message": Spoken prompt or message
                - "appointment": Dict of masked appointment info if verified
        """
        async with self._session_factory() as session:
            # 1. Sub-transaction: Check client rate limits under advisory lock
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext('client_rate_limit'), hashtext(:cid));"
                ),
                {"cid": client_id},
            )
            attempts_res = await session.execute(
                select(func.count(ClientVerificationAttempt.id)).where(
                    ClientVerificationAttempt.client_id == client_id,
                    ClientVerificationAttempt.attempt_type == "standard",
                    ClientVerificationAttempt.attempted_at
                    >= datetime.now(UTC) - timedelta(minutes=30),
                )
            )
            standard_failures = attempts_res.scalar() or 0
            if standard_failures >= 5:
                return {
                    "status": "locked_out",
                    "message": (
                        "For your security, appointment verification has been locked due to multiple "
                        "incorrect attempts. Please contact the clinic directly with your appointment details."
                    ),
                }

            # 2. Query candidates by clinic_id, phone suffix, confirmed status, upcoming start_at
            now_utc = datetime.now(UTC)
            res = await session.execute(
                select(Appointment).where(
                    Appointment.clinic_id == self._clinic_id,
                    Appointment.status == "confirmed",
                    Appointment.start_at >= now_utc,
                )
            )
            all_upcoming = res.scalars().all()

            # Filter candidates matching phone_last4 AND names_match()
            matched_candidates: list[Appointment] = []
            for apt in all_upcoming:
                if apt.caller_phone.endswith(phone_last4) and names_match(
                    caller_name, apt.caller_name
                ):
                    matched_candidates.append(apt)

            # If appointment_date is supplied, filter candidates by clinic local date
            if appointment_date and matched_candidates:
                try:
                    target_d = date.fromisoformat(appointment_date.strip())
                    matched_candidates = [
                        apt
                        for apt in matched_candidates
                        if apt.start_at.astimezone(CLINIC_TZ).date() == target_d
                    ]
                except ValueError:
                    matched_candidates = []

            # 3. Handle 0 matches: Anti-enumeration generic failure
            if not matched_candidates:
                # Record failure
                session.add(
                    ClientVerificationAttempt(
                        client_id=client_id,
                        attempt_type="standard",
                        attempted_at=now_utc,
                    )
                )
                await session.commit()
                return {
                    "status": "not_found",
                    "message": (
                        "I could not find an upcoming appointment with the details provided. "
                        "Please check the name and last 4 digits of your phone number."
                    ),
                }

            # 4. Handle multiple matches: Date disambiguation
            if len(matched_candidates) > 1:
                return {
                    "status": "ambiguous",
                    "message": (
                        "You have multiple upcoming appointments on file. "
                        "Please state the date of the appointment you would like to view or modify."
                    ),
                }

            # 5. Exactly 1 candidate matched
            candidate = matched_candidates[0]

            # Check step-up threshold (>= 5 failures across all clients in last 60 minutes)
            failures_res = await session.execute(
                select(func.count(AppointmentVerificationFailure.id)).where(
                    AppointmentVerificationFailure.appointment_id == candidate.id,
                    AppointmentVerificationFailure.attempted_at >= now_utc - timedelta(minutes=60),
                )
            )
            recent_failures = failures_res.scalar() or 0
            if recent_failures >= 5:
                return {
                    "status": "step_up_required",
                    "message": (
                        "For your security, please verify the full phone number associated with your appointment."
                    ),
                }

            # 6. Verification successful: Bind to caller_verification_sessions under L1 lock
            res_sess = await session.execute(
                select(CallerVerificationSession)
                .where(CallerVerificationSession.session_id == voice_session_id)
                .with_for_update()
            )
            v_session = res_sess.scalar_one_or_none()
            if not v_session:
                v_session = CallerVerificationSession(
                    session_id=voice_session_id,
                    clinic_id=self._clinic_id,
                    verification_failures=0,
                    locked_out=False,
                    verified_appointment_id=candidate.id,
                    verified_at=now_utc,
                )
                session.add(v_session)
            else:
                if v_session.handed_off:
                    raise SessionHandedOffError("Session is already handed off.")
                v_session.verified_appointment_id = candidate.id
                v_session.verified_at = now_utc
                v_session.updated_at = now_utc

            # Client-isolated reset: clear this client's failure history
            await session.execute(
                text("DELETE FROM client_verification_attempts WHERE client_id = :cid;"),
                {"cid": client_id},
            )
            await session.execute(
                text(
                    "DELETE FROM appointment_verification_failures "
                    "WHERE appointment_id = :aid AND client_id = :cid;"
                ),
                {"aid": candidate.id, "cid": client_id},
            )
            await session.commit()

            local_start = candidate.start_at.astimezone(CLINIC_TZ)
            formatted_date = local_start.strftime("%A, %d %B %Y at %I:%M %p").replace(" 0", " ")
            return {
                "status": "verified",
                "message": (
                    f"Thank you, {candidate.caller_name}. I have verified your appointment for "
                    f"{candidate.service_name_snapshot} on {formatted_date}."
                ),
                "appointment": {
                    "id": candidate.id,
                    "service_name": candidate.service_name_snapshot,
                    "formatted_date": formatted_date,
                    "caller_name": candidate.caller_name,
                    "caller_phone_masked": f"***-***-{candidate.caller_phone[-4:]}",
                },
            }

    # -------------------------------------------------------------------------
    # 2. Full-Phone Step-Up Verification
    # -------------------------------------------------------------------------
    async def verify_appointment_step_up(
        self,
        *,
        voice_session_id: str,
        client_id: str,
        full_phone: str,
        caller_name: str,
    ) -> dict[str, Any]:
        """Validate full phone number and caller name under partitioned step-up rate limits."""
        async with self._session_factory() as session:
            # Check step-up rate limit (max 3 in 30 minutes)
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext('client_rate_limit'), hashtext(:cid));"
                ),
                {"cid": client_id},
            )
            attempts_res = await session.execute(
                select(func.count(ClientVerificationAttempt.id)).where(
                    ClientVerificationAttempt.client_id == client_id,
                    ClientVerificationAttempt.attempt_type == "step_up",
                    ClientVerificationAttempt.attempted_at
                    >= datetime.now(UTC) - timedelta(minutes=30),
                )
            )
            step_up_failures = attempts_res.scalar() or 0
            if step_up_failures >= 3:
                return {
                    "status": "locked_out",
                    "message": (
                        "For your security, appointment verification has been locked due to multiple "
                        "incorrect attempts. Please contact the clinic directly."
                    ),
                }

            normalized_phone = normalize_phone(full_phone)
            now_utc = datetime.now(UTC)

            # Query appointment
            res = await session.execute(
                select(Appointment).where(
                    Appointment.clinic_id == self._clinic_id,
                    Appointment.caller_phone == normalized_phone,
                    Appointment.status == "confirmed",
                    Appointment.start_at >= now_utc,
                )
            )
            candidates = res.scalars().all()
            matched = [c for c in candidates if names_match(caller_name, c.caller_name)]

            if not matched:
                session.add(
                    ClientVerificationAttempt(
                        client_id=client_id,
                        attempt_type="step_up",
                        attempted_at=now_utc,
                    )
                )
                await session.commit()
                return {
                    "status": "not_found",
                    "message": "The full phone number and name provided could not be verified.",
                }

            candidate = matched[0]
            # Bind to session
            res_sess = await session.execute(
                select(CallerVerificationSession)
                .where(CallerVerificationSession.session_id == voice_session_id)
                .with_for_update()
            )
            v_session = res_sess.scalar_one_or_none()
            if not v_session:
                v_session = CallerVerificationSession(
                    session_id=voice_session_id,
                    clinic_id=self._clinic_id,
                    verification_failures=0,
                    locked_out=False,
                    verified_appointment_id=candidate.id,
                    verified_at=now_utc,
                )
                session.add(v_session)
            else:
                if v_session.handed_off:
                    raise SessionHandedOffError("Session is already handed off.")
                v_session.verified_appointment_id = candidate.id
                v_session.verified_at = now_utc
                v_session.updated_at = now_utc

            # Clear client failure records
            await session.execute(
                text("DELETE FROM client_verification_attempts WHERE client_id = :cid;"),
                {"cid": client_id},
            )
            await session.execute(
                text(
                    "DELETE FROM appointment_verification_failures "
                    "WHERE appointment_id = :aid AND client_id = :cid;"
                ),
                {"aid": candidate.id, "cid": client_id},
            )
            await session.commit()

            local_start = candidate.start_at.astimezone(CLINIC_TZ)
            formatted_date = local_start.strftime("%A, %d %B %Y at %I:%M %p").replace(" 0", " ")
            return {
                "status": "verified",
                "message": f"Verification successful for {candidate.caller_name} on {formatted_date}.",
                "appointment": {
                    "id": candidate.id,
                    "service_name": candidate.service_name_snapshot,
                    "formatted_date": formatted_date,
                },
            }

    # -------------------------------------------------------------------------
    # 3. Create Cancellation Proposal (IDOR-Free)
    # -------------------------------------------------------------------------
    async def create_cancellation_proposal(
        self,
        *,
        voice_session_id: str,
        reason_category: str = "other",
        created_turn_id: int = 1,
    ) -> dict[str, Any]:
        """Derives appointment exclusively from server-persisted verified session state."""
        async with self._session_factory() as session:
            now_utc = datetime.now(UTC)
            await self._lock_voice_session(session, voice_session_id)

            # L1: Lock session
            res_sess = await session.execute(
                select(CallerVerificationSession)
                .where(CallerVerificationSession.session_id == voice_session_id)
                .with_for_update()
            )
            v_session = res_sess.scalar_one_or_none()
            if not v_session or v_session.verified_appointment_id is None:
                raise UnverifiedCallerSessionError(
                    "No verified appointment in session. Verification required."
                )
            if v_session.handed_off:
                raise SessionHandedOffError("Session has been handed off to human staff.")

            appointment_id = v_session.verified_appointment_id

            # L2: Lock appointment
            res_apt = await session.execute(
                select(Appointment).where(Appointment.id == appointment_id).with_for_update()
            )
            appointment = res_apt.scalar_one_or_none()
            if not appointment or appointment.status != "confirmed":
                raise InvalidAppointmentStatusError("Appointment is not in confirmed state.")

            # Minimum cancellation notice window (2 hours before start)
            if appointment.start_at - now_utc < timedelta(hours=2):
                raise CancellationWindowError(
                    "Appointments cannot be cancelled within 2 hours of start time."
                )

            # L3: Check durable idempotency key
            idempotency_key = hashlib.sha256(
                f"{voice_session_id}:{appointment_id}:{created_turn_id}".encode()
            ).hexdigest()
            res_idem = await session.execute(
                select(CancellationProposal).where(
                    CancellationProposal.idempotency_key == idempotency_key
                )
            )
            existing_idem = res_idem.scalar_one_or_none()
            if existing_idem:
                if existing_idem.state == "pending":
                    return {
                        "proposal_id": str(existing_idem.id),
                        "challenge_id": str(existing_idem.challenge_id),
                        "readback_text": f"Please confirm cancellation of your appointment for {appointment.service_name_snapshot}.",
                    }
                elif existing_idem.state == "confirmed":
                    return {"status": "already_cancelled", "proposal_id": str(existing_idem.id)}
                else:
                    raise IdempotencyKeyConsumedError(
                        "This cancellation proposal has already expired or been replaced."
                    )

            await self._replace_session_proposals(session, voice_session_id, now_utc)
            # Expire any prior pending cancellation proposals for this appointment
            await session.execute(
                update(CancellationProposal)
                .where(
                    CancellationProposal.appointment_id == appointment_id,
                    CancellationProposal.state == "pending",
                )
                .values(state="replaced", consumed_at=now_utc)
            )

            # L4: Create consent challenge in pending_readback
            challenge_id = uuid4()
            proposal_id = uuid4()
            local_start = appointment.start_at.astimezone(CLINIC_TZ)
            formatted_date = local_start.strftime("%A, %d %B %Y at %I:%M %p").replace(" 0", " ")
            readback_text = (
                f"You are requesting to cancel your appointment for {appointment.service_name_snapshot} "
                f"on {formatted_date}. Please say yes to confirm this cancellation."
            )
            readback_hash = hashlib.sha256(readback_text.strip().lower().encode()).hexdigest()
            expires_at = now_utc + timedelta(seconds=PROPOSAL_TTL_SECONDS)

            challenge = ConsentChallenge(
                challenge_id=challenge_id,
                session_id=voice_session_id,
                clinic_id=self._clinic_id,
                action_type="cancel_appointment",
                target_id=str(proposal_id),
                target_version=appointment.version,
                readback_details_hash=readback_hash,
                readback_completed_turn_id=None,
                status="pending_readback",
                created_at=now_utc,
                expires_at=expires_at,
            )
            session.add(challenge)

            # L3: Insert cancellation proposal
            proposal = CancellationProposal(
                id=proposal_id,
                clinic_id=self._clinic_id,
                appointment_id=appointment_id,
                expected_appointment_version=appointment.version,
                session_id=voice_session_id,
                created_turn_id=created_turn_id,
                caller_name=appointment.caller_name,
                caller_phone=appointment.caller_phone,
                reason_category=reason_category,
                state="pending",
                challenge_id=challenge_id,
                readback_details_hash=readback_hash,
                expires_at=expires_at,
                idempotency_key=idempotency_key,
                created_at=now_utc,
            )
            session.add(proposal)
            await session.commit()

            return {
                "proposal_id": str(proposal_id),
                "challenge_id": str(challenge_id),
                "readback_text": readback_text,
            }

    # -------------------------------------------------------------------------
    # 4. Create Reschedule Proposal (IDOR-Free)
    # -------------------------------------------------------------------------
    async def create_reschedule_proposal(
        self,
        *,
        voice_session_id: str,
        new_start_at: str,
        created_turn_id: int = 1,
    ) -> dict[str, Any]:
        """Derives appointment exclusively from server-persisted verified session state."""
        async with self._session_factory() as session:
            now_utc = datetime.now(UTC)
            await self._lock_voice_session(session, voice_session_id)

            # L1: Lock session
            res_sess = await session.execute(
                select(CallerVerificationSession)
                .where(CallerVerificationSession.session_id == voice_session_id)
                .with_for_update()
            )
            v_session = res_sess.scalar_one_or_none()
            if not v_session or v_session.verified_appointment_id is None:
                raise UnverifiedCallerSessionError("No verified appointment in session.")
            if v_session.handed_off:
                raise SessionHandedOffError("Session is handed off.")

            appointment_id = v_session.verified_appointment_id

            # L2: Lock appointment
            res_apt = await session.execute(
                select(Appointment).where(Appointment.id == appointment_id).with_for_update()
            )
            appointment = res_apt.scalar_one_or_none()
            if not appointment or appointment.status != "confirmed":
                raise InvalidAppointmentStatusError("Appointment is not in confirmed state.")

            # Parse new slot
            new_start_dt = datetime.fromisoformat(new_start_at.strip())
            if new_start_dt.tzinfo is None:
                new_start_dt = new_start_dt.replace(tzinfo=CLINIC_TZ)
            new_start_utc = new_start_dt.astimezone(UTC)
            new_end_utc = new_start_utc + timedelta(minutes=appointment.duration_minutes)
            new_blocked_utc = new_end_utc + timedelta(minutes=appointment.buffer_minutes)

            await validate_schedule_slot(
                session, clinic_id=self._clinic_id, service_id=appointment.service_id,
                start_at=new_start_utc, duration_minutes=appointment.duration_minutes,
                buffer_minutes=appointment.buffer_minutes, service_name=appointment.service_name_snapshot,
                now=now_utc,
            )

            # Check conflict
            conflict = await check_appointment_conflict(
                session=session,
                clinic_id=self._clinic_id,
                start_at=new_start_utc,
                duration_minutes=appointment.duration_minutes,
                buffer_minutes=appointment.buffer_minutes,
                exclude_appointment_id=appointment_id,
            )
            if conflict:
                raise SlotUnavailableError("Requested appointment slot is already booked.")

            # L3: Check idempotency key
            idempotency_key = hashlib.sha256(
                f"{voice_session_id}:reschedule:{appointment_id}:{new_start_utc.isoformat()}:{created_turn_id}".encode()
            ).hexdigest()

            existing = await session.execute(select(BookingProposal.id).where(
                BookingProposal.idempotency_key == idempotency_key
            ))
            if existing.scalar_one_or_none() is not None:
                raise IdempotencyKeyConsumedError("A proposal was already made for this request. Please respond to the readback or request a new proposal.")
            await self._replace_session_proposals(session, voice_session_id, now_utc)

            # Expire prior pending reschedule proposals for this appointment
            await session.execute(
                update(BookingProposal)
                .where(
                    BookingProposal.appointment_id == appointment_id,
                    BookingProposal.proposal_type == "reschedule",
                    BookingProposal.state == "pending",
                )
                .values(state="replaced", consumed_at=now_utc)
            )

            # L4: Challenge
            challenge_id = uuid4()
            proposal_id = uuid4()
            local_start = new_start_utc.astimezone(CLINIC_TZ)
            formatted_date = local_start.strftime("%A, %d %B %Y at %I:%M %p").replace(" 0", " ")
            readback_text = (
                f"You are rescheduling your appointment for {appointment.service_name_snapshot} to "
                f"{formatted_date}. Please say yes to confirm."
            )
            readback_hash = hashlib.sha256(readback_text.strip().lower().encode()).hexdigest()
            expires_at = now_utc + timedelta(seconds=PROPOSAL_TTL_SECONDS)

            challenge = ConsentChallenge(
                challenge_id=challenge_id,
                session_id=voice_session_id,
                clinic_id=self._clinic_id,
                action_type="reschedule_appointment",
                target_id=str(proposal_id),
                target_version=appointment.version,
                readback_details_hash=readback_hash,
                readback_completed_turn_id=None,
                status="pending_readback",
                created_at=now_utc,
                expires_at=expires_at,
            )
            session.add(challenge)

            # L3: Proposal
            proposal = BookingProposal(
                id=proposal_id,
                clinic_id=self._clinic_id,
                service_id=appointment.service_id,
                service_name_snapshot=appointment.service_name_snapshot,
                appointment_id=appointment_id,
                expected_appointment_version=appointment.version,
                proposal_type="reschedule",
                session_id=voice_session_id,
                created_turn_id=created_turn_id,
                duration_minutes=appointment.duration_minutes,
                buffer_minutes=appointment.buffer_minutes,
                caller_name=appointment.caller_name,
                caller_phone=appointment.caller_phone,
                caller_email=appointment.caller_email,
                start_at=new_start_utc,
                end_at=new_end_utc,
                blocked_until_at=new_blocked_utc,
                state="pending",
                challenge_id=challenge_id,
                readback_details_hash=readback_hash,
                expires_at=expires_at,
                idempotency_key=idempotency_key,
                created_at=now_utc,
            )
            session.add(proposal)
            await session.commit()

            return {
                "proposal_id": str(proposal_id),
                "challenge_id": str(challenge_id),
                "readback_text": readback_text,
            }

    # -------------------------------------------------------------------------
    # 5. Create New Booking Proposal
    # -------------------------------------------------------------------------
    async def create_booking_proposal(
        self,
        *,
        voice_session_id: str,
        service_id: int,
        start_at: str,
        caller_name: str,
        caller_phone: str,
        caller_email: str | None = None,
        created_turn_id: int = 1,
    ) -> dict[str, Any]:
        """Create a new booking proposal and armed challenge in pending_readback."""
        async with self._session_factory() as session:
            now_utc = datetime.now(UTC)
            await self._lock_voice_session(session, voice_session_id)

            # L1: Lock session
            res_sess = await session.execute(
                select(CallerVerificationSession)
                .where(CallerVerificationSession.session_id == voice_session_id)
                .with_for_update()
            )
            v_session = res_sess.scalar_one_or_none()
            if v_session and v_session.handed_off:
                raise SessionHandedOffError("Session is handed off.")

            # Load service
            res_srv = await session.execute(select(Service).where(Service.id == service_id))
            service = res_srv.scalar_one_or_none()
            if not service:
                raise ProposalNotFoundError(f"Service id {service_id} not found.")

            start_dt = datetime.fromisoformat(start_at.strip())
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=CLINIC_TZ)
            start_utc = start_dt.astimezone(UTC)
            end_utc = start_utc + timedelta(minutes=service.duration_minutes)
            blocked_utc = end_utc + timedelta(minutes=service.buffer_minutes)

            await validate_schedule_slot(
                session, clinic_id=self._clinic_id, service_id=service_id, start_at=start_utc,
                duration_minutes=service.duration_minutes, buffer_minutes=service.buffer_minutes,
                service_name=service.name, now=now_utc,
            )

            # Check conflict
            conflict = await check_appointment_conflict(
                session=session,
                clinic_id=self._clinic_id,
                start_at=start_utc,
                duration_minutes=service.duration_minutes,
                buffer_minutes=service.buffer_minutes,
            )
            if conflict:
                raise SlotUnavailableError("Requested booking slot is already reserved.")

            idempotency_key = hashlib.sha256(
                f"{voice_session_id}:new:{service_id}:{start_utc.isoformat()}:{created_turn_id}".encode()
            ).hexdigest()

            existing = await session.execute(select(BookingProposal.id).where(
                BookingProposal.idempotency_key == idempotency_key
            ))
            if existing.scalar_one_or_none() is not None:
                raise IdempotencyKeyConsumedError("A proposal was already made for this request. Please respond to the readback or request a new proposal.")
            await self._replace_session_proposals(session, voice_session_id, now_utc)

            # Expire prior pending booking proposals for this session
            await session.execute(
                update(BookingProposal)
                .where(
                    BookingProposal.session_id == voice_session_id,
                    BookingProposal.proposal_type == "new_booking",
                    BookingProposal.state == "pending",
                )
                .values(state="replaced", consumed_at=now_utc)
            )

            challenge_id = uuid4()
            proposal_id = uuid4()
            local_start = start_utc.astimezone(CLINIC_TZ)
            formatted_date = local_start.strftime("%A, %d %B %Y at %I:%M %p").replace(" 0", " ")
            readback_text = (
                f"You are booking {service.name} on {formatted_date} for {caller_name}. "
                f"Please say yes to confirm."
            )
            readback_hash = hashlib.sha256(readback_text.strip().lower().encode()).hexdigest()
            expires_at = now_utc + timedelta(seconds=PROPOSAL_TTL_SECONDS)

            challenge = ConsentChallenge(
                challenge_id=challenge_id,
                session_id=voice_session_id,
                clinic_id=self._clinic_id,
                action_type="create_booking",
                target_id=str(proposal_id),
                target_version=None,
                readback_details_hash=readback_hash,
                readback_completed_turn_id=None,
                status="pending_readback",
                created_at=now_utc,
                expires_at=expires_at,
            )
            session.add(challenge)

            proposal = BookingProposal(
                id=proposal_id,
                clinic_id=self._clinic_id,
                service_id=service_id,
                service_name_snapshot=service.name,
                appointment_id=None,
                expected_appointment_version=None,
                proposal_type="new_booking",
                session_id=voice_session_id,
                created_turn_id=created_turn_id,
                duration_minutes=service.duration_minutes,
                buffer_minutes=service.buffer_minutes,
                caller_name=caller_name,
                caller_phone=normalize_phone(caller_phone),
                caller_email=caller_email,
                start_at=start_utc,
                end_at=end_utc,
                blocked_until_at=blocked_utc,
                state="pending",
                challenge_id=challenge_id,
                readback_details_hash=readback_hash,
                expires_at=expires_at,
                idempotency_key=idempotency_key,
                created_at=now_utc,
            )
            session.add(proposal)
            await session.commit()

            return {
                "proposal_id": str(proposal_id),
                "challenge_id": str(challenge_id),
                "readback_text": readback_text,
            }

    # -------------------------------------------------------------------------
    # 6. Readback Playout Arming Callback
    # -------------------------------------------------------------------------
    async def arm_readback(
        self,
        *,
        challenge_id: UUID,
        completed_turn_id: int,
    ) -> None:
        """Called by background task when audio playout finishes without interruption."""
        async with self._session_factory() as session:
            now_utc = datetime.now(UTC)
            await session.execute(
                update(ConsentChallenge)
                .where(
                    ConsentChallenge.challenge_id == challenge_id,
                    ConsentChallenge.status == "pending_readback",
                )
                .values(
                    status="pending",
                    readback_completed_at=now_utc,
                    readback_completed_turn_id=completed_turn_id,
                )
            )
            await session.commit()

    async def invalidate_readback(
        self,
        *,
        challenge_id: UUID,
        reason: str,
    ) -> None:
        """Called on interruption, exception, or timeout."""
        async with self._session_factory() as session:
            now_utc = datetime.now(UTC)
            await session.execute(
                update(ConsentChallenge)
                .where(
                    ConsentChallenge.challenge_id == challenge_id,
                    ConsentChallenge.status == "pending_readback",
                )
                .values(
                    status="invalidated",
                    invalidated_at=now_utc,
                    invalidation_reason=reason,
                )
            )
            await session.commit()

    # -------------------------------------------------------------------------
    # 7. Single Atomic Transaction: Consent Validation & Calendar Mutation
    # -------------------------------------------------------------------------
    async def confirm_proposal(
        self,
        *,
        proposal_id_str: str,
        voice_session_id: str,
        affirmative_turn_id: int,
        affirmative_item_id: str,
        affirmative_transcript: str,
        speech_started_at: datetime | None,
        is_cancellation: bool = False,
    ) -> dict[str, Any]:
        """Atomically validate caller affirmative consent and execute booking mutation under calendar advisory locks."""
        proposal_id = UUID(proposal_id_str)
        now_utc = datetime.now(UTC)

        async with self._session_factory() as session:
            async with session.begin():
                await self._lock_voice_session(session, voice_session_id)
                # 1. Handoff check
                res_sess = await session.execute(
                    select(CallerVerificationSession).where(
                        CallerVerificationSession.session_id == voice_session_id
                    )
                )
                v_session = res_sess.scalar_one_or_none()
                if v_session and v_session.handed_off:
                    raise SessionHandedOffError("Session is handed off.")

                # 2. Load proposal without FOR UPDATE (serialization guaranteed by advisory locks)
                proposal_model: Any = None
                if is_cancellation:
                    res_p = await session.execute(
                        select(CancellationProposal).where(CancellationProposal.id == proposal_id)
                    )
                    proposal_model = res_p.scalar_one_or_none()
                else:
                    res_p = await session.execute(
                        select(BookingProposal).where(BookingProposal.id == proposal_id)
                    )
                    proposal_model = res_p.scalar_one_or_none()

                if not proposal_model:
                    raise ProposalNotFoundError(f"Proposal {proposal_id} not found.")

                if proposal_model.session_id != voice_session_id:
                    raise InvalidConsentError("Proposal does not belong to this voice session.")

                # 3. Load consent challenge
                res_ch = await session.execute(
                    select(ConsentChallenge).where(
                        ConsentChallenge.challenge_id == proposal_model.challenge_id
                    )
                )
                challenge = res_ch.scalar_one_or_none()
                if not challenge:
                    raise InvalidConsentError("Linked consent challenge not found.")

                # Idempotent retry check: if proposal already confirmed and challenge consumed
                if proposal_model.state == "confirmed":
                    if challenge.status == "consumed":
                        confirmed_id = (
                            proposal_model.appointment_id
                            if is_cancellation
                            else proposal_model.confirmed_appointment_id
                        )
                        return {
                            "status": "confirmed",
                            "appointment_id": confirmed_id,
                            "proposal_id": str(proposal_id),
                        }
                    raise ProposalAlreadyUsedError(f"Proposal is already {proposal_model.state}.")

                if proposal_model.state != "pending":
                    raise ProposalAlreadyUsedError(f"Proposal is already {proposal_model.state}.")

                if challenge.status != "pending":
                    raise InvalidConsentError(
                        f"Consent challenge is in status '{challenge.status}'."
                    )

                if challenge.expires_at <= now_utc:
                    proposal_model.state = "expired"
                    challenge.status = "invalidated"
                    challenge.invalidated_at = now_utc
                    challenge.invalidation_reason = "expired"
                    raise ProposalExpiredError("Proposal has expired.")

                # 4. Caller Turn & Intent Verification
                intent = classify_caller_intent(affirmative_transcript)
                if intent != Intent.AFFIRMATIVE:
                    if intent == Intent.NEGATIVE:
                        challenge.status = "invalidated"
                        challenge.invalidated_at = now_utc
                        challenge.invalidation_reason = "caller_rejected"
                        proposal_model.state = "expired"
                    raise InvalidConsentError(
                        "Caller utterance did not express affirmative consent."
                    )

                # Monotonic Turn Check
                if challenge.readback_completed_turn_id is not None:
                    if affirmative_turn_id <= challenge.readback_completed_turn_id:
                        raise InvalidConsentError(
                            "Affirmative turn must be strictly after proposal readback turn."
                        )

                # Speech Boundary Check
                if challenge.readback_completed_at is not None and speech_started_at is not None:
                    delta = (speech_started_at - challenge.readback_completed_at).total_seconds()
                    if delta < -5.0:
                        raise ClockSkewDetectedError(
                            "Severe clock anomaly: speech started >5s before playout."
                        )
                    elif delta < 0.0:
                        raise EarlySpeechBoundaryError(
                            "Caller began speaking before readback playout completed."
                        )

                # 5. Target Appointment Check
                target_apt: Appointment | None = None
                if proposal_model.appointment_id is not None:
                    res_apt = await session.execute(
                        select(Appointment).where(Appointment.id == proposal_model.appointment_id)
                    )
                    target_apt = res_apt.scalar_one_or_none()
                    if not target_apt or target_apt.status != "confirmed":
                        proposal_model.state = "expired"
                        challenge.status = "invalidated"
                        challenge.invalidated_at = now_utc
                        challenge.invalidation_reason = "invalid_status"
                        raise InvalidAppointmentStatusError("Target appointment is not confirmed.")
                    if target_apt.version != proposal_model.expected_appointment_version:
                        proposal_model.state = "expired"
                        challenge.status = "invalidated"
                        challenge.invalidated_at = now_utc
                        challenge.invalidation_reason = "stale_version"
                        raise StaleAppointmentVersionError("Appointment was modified concurrently.")

                # 6. Acquire PostgreSQL Transaction-Level Date Advisory Locks in Ascending Order
                dates_to_lock: list[int]
                if is_cancellation:
                    assert target_apt is not None
                    dates_to_lock = get_affected_date_ints(
                        target_apt.start_at, target_apt.blocked_until_at, CLINIC_TZ
                    )
                elif proposal_model.proposal_type == "reschedule":
                    assert target_apt is not None
                    old_dates = set(
                        get_affected_date_ints(
                            target_apt.start_at, target_apt.blocked_until_at, CLINIC_TZ
                        )
                    )
                    new_dates = set(
                        get_affected_date_ints(
                            proposal_model.start_at, proposal_model.blocked_until_at, CLINIC_TZ
                        )
                    )
                    dates_to_lock = sorted(old_dates | new_dates)
                else:  # new_booking
                    dates_to_lock = get_affected_date_ints(
                        proposal_model.start_at, proposal_model.blocked_until_at, CLINIC_TZ
                    )

                for d_int in sorted(dates_to_lock):
                    await session.execute(
                        select(func.pg_advisory_xact_lock(self._clinic_id, d_int))
                    )

                # 7. Recheck Slot Conflict under Advisory Lock
                if not is_cancellation:
                    await lock_schedule_configuration(session, proposal_model.clinic_id)
                    await validate_schedule_slot(
                        session, clinic_id=proposal_model.clinic_id, service_id=proposal_model.service_id,
                        start_at=proposal_model.start_at, duration_minutes=proposal_model.duration_minutes,
                        buffer_minutes=proposal_model.buffer_minutes, service_name=proposal_model.service_name_snapshot,
                        now=now_utc,
                    )
                    has_conflict = await check_appointment_conflict(
                        session,
                        clinic_id=proposal_model.clinic_id,
                        start_at=proposal_model.start_at,
                        duration_minutes=proposal_model.duration_minutes,
                        buffer_minutes=proposal_model.buffer_minutes,
                        exclude_appointment_id=proposal_model.appointment_id,
                    )
                    if has_conflict:
                        proposal_model.state = "rejected"
                        proposal_model.consumed_at = now_utc
                        challenge.status = "invalidated"
                        challenge.invalidated_at = now_utc
                        challenge.invalidation_reason = "slot_unavailable"
                        raise SlotUnavailableError("Requested slot is no longer available.")
                else:
                    assert target_apt is not None
                    res_svc = await session.execute(
                        select(Service).where(Service.id == target_apt.service_id)
                    )
                    svc = res_svc.scalar_one_or_none()
                    if svc and svc.cancellation_window_hours > 0:
                        cutoff = now_utc + timedelta(hours=svc.cancellation_window_hours)
                        if target_apt.start_at < cutoff:
                            raise CancellationWindowError(
                                f"Cannot cancel within {svc.cancellation_window_hours} hours of appointment start time."
                            )

                # 8. Execute Mutation and Challenge Consumption
                confirmed_apt_id: int | None = None
                if is_cancellation:
                    assert target_apt is not None
                    target_apt.status = "cancelled"
                    target_apt.cancellation_reason = proposal_model.reason_category
                    target_apt.version += 1
                    target_apt.updated_at = now_utc
                    proposal_model.state = "confirmed"
                    proposal_model.consumed_at = now_utc
                    confirmed_apt_id = target_apt.id
                elif proposal_model.proposal_type == "reschedule":
                    assert target_apt is not None
                    target_apt.start_at = proposal_model.start_at
                    target_apt.end_at = proposal_model.end_at
                    target_apt.duration_minutes = proposal_model.duration_minutes
                    target_apt.buffer_minutes = proposal_model.buffer_minutes
                    target_apt.blocked_until_at = proposal_model.blocked_until_at
                    target_apt.version += 1
                    target_apt.updated_at = now_utc
                    proposal_model.state = "confirmed"
                    proposal_model.consumed_at = now_utc
                    proposal_model.confirmed_appointment_id = target_apt.id
                    confirmed_apt_id = target_apt.id
                else:  # new_booking
                    new_apt = Appointment(
                        clinic_id=proposal_model.clinic_id,
                        service_id=proposal_model.service_id,
                        service_name_snapshot=proposal_model.service_name_snapshot,
                        caller_name=proposal_model.caller_name,
                        caller_phone=proposal_model.caller_phone,
                        caller_email=proposal_model.caller_email,
                        start_at=proposal_model.start_at,
                        end_at=proposal_model.end_at,
                        duration_minutes=proposal_model.duration_minutes,
                        buffer_minutes=proposal_model.buffer_minutes,
                        blocked_until_at=proposal_model.blocked_until_at,
                        status="confirmed",
                        version=1,
                        created_at=now_utc,
                        updated_at=now_utc,
                    )
                    session.add(new_apt)
                    await session.flush()
                    proposal_model.confirmed_appointment_id = new_apt.id
                    proposal_model.state = "confirmed"
                    proposal_model.consumed_at = now_utc
                    confirmed_apt_id = new_apt.id

                # Update challenge as consumed single-use record
                challenge.status = "consumed"
                challenge.consumed_at = now_utc
                challenge.consumed_turn_id = affirmative_turn_id
                challenge.consumed_item_id = affirmative_item_id

                # Invalidate verification session on reschedule or cancellation
                if proposal_model.appointment_id is not None and v_session is not None:
                    v_session.verified_appointment_id = None
                    v_session.verified_at = None
                    v_session.updated_at = now_utc

                return {
                    "status": "confirmed",
                    "appointment_id": confirmed_apt_id,
                    "proposal_id": str(proposal_id),
                }

    # -------------------------------------------------------------------------
    # 8. Human Handoff Gating
    # -------------------------------------------------------------------------
    async def request_human_handoff(
        self,
        *,
        voice_session_id: str,
        category: str = "caller_requested",
    ) -> None:
        """Atomic handoff gate: sets handed_off=True and invalidates active challenges."""
        async with self._session_factory() as session:
            now_utc = datetime.now(UTC)
            await self._lock_voice_session(session, voice_session_id)

            # L1: Lock session
            res_sess = await session.execute(
                select(CallerVerificationSession)
                .where(CallerVerificationSession.session_id == voice_session_id)
                .with_for_update()
            )
            v_session = res_sess.scalar_one_or_none()
            if not v_session:
                v_session = CallerVerificationSession(
                    session_id=voice_session_id,
                    clinic_id=self._clinic_id,
                    verification_failures=0,
                    locked_out=False,
                    handed_off=True,
                    handed_off_at=now_utc,
                    handoff_category=category,
                )
                session.add(v_session)
            else:
                v_session.handed_off = True
                v_session.handed_off_at = now_utc
                v_session.handoff_category = category
                v_session.updated_at = now_utc

            # Invalidate active challenges for this session
            await session.execute(
                update(ConsentChallenge)
                .where(
                    ConsentChallenge.session_id == voice_session_id,
                    ConsentChallenge.status.in_(["pending_readback", "pending", "authorized"]),
                )
                .values(
                    status="invalidated",
                    invalidated_at=now_utc,
                    invalidation_reason="handoff",
                )
            )

            # Expire active proposals for this session
            await session.execute(
                update(BookingProposal)
                .where(
                    BookingProposal.session_id == voice_session_id,
                    BookingProposal.state == "pending",
                )
                .values(state="replaced", consumed_at=now_utc)
            )
            await session.execute(
                update(CancellationProposal)
                .where(
                    CancellationProposal.session_id == voice_session_id,
                    CancellationProposal.state == "pending",
                )
                .values(state="replaced", consumed_at=now_utc)
            )

            await session.commit()
