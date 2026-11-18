"""Typed LiveKit function tools for Phase 5 Voice Agent.

Implements all 13 typed @llm.function_tool definitions using native LiveKit RunContext:
- get_clinic_info
- list_services
- check_availability
- create_booking_proposal
- confirm_booking
- lookup_appointment
- verify_appointment_step_up
- create_reschedule_proposal
- confirm_reschedule
- create_cancellation_proposal
- confirm_cancellation
- request_human_handoff
- escalate_medical_emergency
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any
from uuid import UUID

from livekit.agents import llm
from livekit.agents.voice import RunContext
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.booking import Appointment
from app.repositories.clinic_repo import (
    get_active_services_by_clinic_id,
    get_hours_by_clinic_id,
    get_profile_by_id,
    get_upcoming_closures_by_clinic_id,
)
from app.schemas.dto import ClinicContextDTO
from app.services.availability import compute_available_slots
from app.services.booking_errors import (
    InvalidConsentError,
    ProposalExpiredError,
    SlotUnavailableError,
    StaleAppointmentVersionError,
)
from app.services.clinic_lookup import lookup_clinic_info, lookup_clinic_services
from app.services.voice_booking_service import (
    EarlySpeechBoundaryError,
    IdempotencyKeyConsumedError,
    SessionHandedOffError,
    UnverifiedCallerSessionError,
    VoiceBookingService,
)
from app.voice.context import resolve_clinic_timezone
from app.voice.context_traversal import resolve_causal_parent_user_turn

logger = logging.getLogger("voxora.voice.tools")
_trace_result: ContextVar[tuple[str, str | None] | None] = ContextVar("tool_trace_result", default=None)


def _mark_trace(result: str, reason: str | None = None) -> None:
    if _trace_result.get() is not None:
        _trace_result.set((result, reason))


def _mark_failure(exc: Exception) -> None:
    if isinstance(exc, SlotUnavailableError):
        reason = "slot_conflict"
    elif isinstance(exc, ProposalExpiredError):
        reason = "proposal_expired"
    elif isinstance(exc, (EarlySpeechBoundaryError, InvalidConsentError)):
        reason = "confirmation_rejected"
    elif isinstance(exc, UnverifiedCallerSessionError):
        reason = "verification_required"
    elif isinstance(exc, StaleAppointmentVersionError):
        reason = "stale_appointment"
    else:
        reason = "operation_failed"
    _mark_trace("failed", reason)

DEMO_HANDOFF_SCRIPTS = {
    "caller_requested": (
        "Because this is a demonstration system for the Voxora voice AI receptionist, "
        "live telephone transfer is not available. In a production clinic, this call would now transfer to "
        "reception staff. If you need assistance, please consult your healthcare provider directly."
    ),
    "caller_distress": (
        "I sense that this conversation may be better handled by a human staff member. "
        "Because this is a demonstration system, live transfer is not available. "
        "Please reach out to the clinic reception directly during business hours."
    ),
    "unresolved_query": (
        "Because this is a demonstration system, I cannot fully resolve this request and "
        "live telephone transfer is not available. In a production clinic, this call would transfer to "
        "reception staff. Please contact the clinic directly during business hours."
    ),
    "provider_failure": (
        "I am encountering a temporary service failure with voice processing. "
        "Live telephone transfer is not available in this demo. "
        "Please contact the clinic directly to complete your request."
    ),
    "system_error": (
        "I am experiencing a system database error and cannot complete your request. "
        "Live transfer is not available in this demo. "
        "Please call the clinic directly to manage your appointment."
    ),
    "verification_lockout": (
        "For your security, appointment verification has been locked due to multiple incorrect attempts. "
        "Live telephone transfer is not available in this demo. "
        "Please contact the clinic directly with your appointment details."
    ),
    "medical_emergency": (
        "If you are experiencing a medical emergency or urgent symptoms, please contact your local "
        "emergency services immediately or consult a qualified healthcare provider. As an automated receptionist, "
        "I cannot provide medical evaluation, treatment advice, or emergency triage. "
        "Please seek immediate professional medical attention."
    ),
    "out_of_scope": (
        "I cannot assist with that request as it falls outside clinic scheduling. "
        "Live transfer is not available in this demo. Please consult your physician or clinic staff directly."
    ),
}


class TurnBudgetExceededError(Exception):
    """Raised when a voice session exceeds the maximum allowable turn budget (100 turns)."""


class ClinicVoiceTools:
    """Encapsulates the 13 typed voice tools bound to session and domain service."""

    def __init__(
        self,
        service: VoiceBookingService,
        session_factory: async_sessionmaker[AsyncSession],
        clinic_ctx: ClinicContextDTO,
        voice_session_id: str,
        client_id: str,
        trace: Any | None = None,
    ):
        self.service = service
        self.session_factory = session_factory
        self.clinic_ctx = clinic_ctx
        self.voice_session_id = voice_session_id
        self.client_id = client_id
        self.trace = trace
        self._turn_tracker: dict[str, int] = {}
        self._turn_counter = 1
        self._pending_proposal: tuple[str, str] | None = None
        self._readback_task: asyncio.Task[None] | None = None

    def record_turn(self, item_id: str) -> int:
        """Assign and track a monotonic turn ID for each user turn.

        Enforces a fail-closed session budget cap of 100 turns.
        """
        if item_id not in self._turn_tracker:
            if self._turn_counter > 100:
                raise TurnBudgetExceededError("Session exceeded maximum turn budget of 100 turns.")
            self._turn_tracker[item_id] = self._turn_counter
            self._turn_counter += 1
        return self._turn_tracker[item_id]

    def get_tools(self) -> list[Any]:
        """Return list of all 13 bound tool functions for Agent(tools=...)."""
        tools = [
            self.get_clinic_info,
            self.list_services,
            self.check_availability,
            self.create_booking_proposal,
            self.confirm_booking,
            self.lookup_appointment,
            self.verify_appointment_step_up,
            self.create_reschedule_proposal,
            self.confirm_reschedule,
            self.create_cancellation_proposal,
            self.confirm_cancellation,
            self.request_human_handoff,
            self.escalate_medical_emergency,
        ]
        return [self._instrument(tool) for tool in tools] if self.trace else tools

    def _instrument(self, tool: Any) -> Any:
        """Measure the actual tool call without recording arguments or response text."""
        trace = self.trace
        assert trace is not None
        @functools.wraps(tool)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                trace.record_tool_started(tool.__name__)
            except Exception:
                logger.warning("Unable to record tool start", exc_info=False)
            token = _trace_result.set(("completed", None))
            try:
                return await tool(*args, **kwargs)
            except llm.StopResponse:
                _mark_trace("readback")
                raise
            except Exception:
                _mark_trace("error", "unexpected_error")
                raise
            finally:
                result, reason = _trace_result.get() or ("error", "unexpected_error")
                _trace_result.reset(token)
                try:
                    trace.record_tool(tool.__name__, result, (time.perf_counter() - started) * 1000,
                                      reason if result in {"failed", "error"} else None)
                except Exception:
                    logger.warning("Unable to record tool trace", exc_info=False)

        info = tool.info
        return llm.function_tool(wrapped, name=info.name, description=info.description,
                                 flags=info.flags, on_duplicate=info.on_duplicate,
                                 duplicate_scope=info.duplicate_scope)

    @llm.function_tool()
    async def get_clinic_info(
        self, ctx: RunContext[Any], topic: str, on_date: str | None = None
    ) -> str:
        """Get current clinic facts, policies or hours; use on_date YYYY-MM-DD for a specific date."""
        try:
            async with asyncio.timeout(5):
                async with self.session_factory() as session:
                    return await lookup_clinic_info(
                        session, profile_id_or_1(self.clinic_ctx), topic, on_date
                    )
        except (SQLAlchemyError, OSError, TimeoutError):
            _mark_trace("failed", "database_unavailable")
            logger.warning("Clinic information lookup unavailable")
            return "Clinic information is temporarily unavailable. Please contact reception."

    @llm.function_tool()
    async def list_services(self, ctx: RunContext[Any], service_name: str | None = None) -> str:
        """List active services and prices, or get details for one named active service."""
        try:
            async with asyncio.timeout(5):
                async with self.session_factory() as session:
                    return await lookup_clinic_services(
                        session, profile_id_or_1(self.clinic_ctx), service_name
                    )
        except (SQLAlchemyError, OSError, TimeoutError):
            _mark_trace("failed", "database_unavailable")
            logger.warning("Clinic services lookup unavailable")
            return "Clinic services are temporarily unavailable. Please contact reception."

    @llm.function_tool()
    async def check_availability(
        self,
        ctx: RunContext[Any],
        service_id: int,
        start_date: str,
        days: int = 7,
    ) -> str:
        """Check available booking slots starting from start_date (YYYY-MM-DD) for a given service."""
        try:
            d = date.fromisoformat(start_date.strip())
            end_date = d + timedelta(days=max(1, min(days, 14)) - 1)
            if d.year < 2 or end_date.year > 9998:
                raise ValueError("date range out of bounds")
        except (ValueError, OverflowError):
            return "Please provide a valid start date in YYYY-MM-DD format."

        try:
            async with asyncio.timeout(5):
                async with self.session_factory() as session:
                    clinic_id = profile_id_or_1(self.clinic_ctx)
                    profile = await get_profile_by_id(session, clinic_id)
                    if profile is None:
                        return "Clinic availability is temporarily unavailable. Please contact reception."
                    services = await get_active_services_by_clinic_id(session, clinic_id)
                    srv = next((item for item in services if item.id == service_id), None)
                    if srv is None:
                        return f"Active service ID {service_id} not found."
                    tz = resolve_clinic_timezone(profile.timezone)
                    hours = await get_hours_by_clinic_id(session, clinic_id)
                    closures = await get_upcoming_closures_by_clinic_id(session, clinic_id, d, end_date)
                    start = datetime.combine(d, datetime_time.min, tzinfo=tz).astimezone(UTC)
                    end = datetime.combine(end_date + timedelta(days=1), datetime_time.min, tzinfo=tz).astimezone(UTC)
                    result = await session.execute(select(Appointment).where(
                        Appointment.clinic_id == clinic_id, Appointment.status == "confirmed",
                        Appointment.start_at < end, Appointment.blocked_until_at > start))
                    slots = compute_available_slots(
                        clinic_id=clinic_id, service=srv, hours=hours, closures=closures,
                        existing_appointments=list(result.scalars().all()), start_date=d,
                        end_date=end_date, clinic_tz=tz, now_utc=datetime.now(UTC),
                    )
        except (SQLAlchemyError, OSError, TimeoutError):
            _mark_trace("failed", "database_unavailable")
            logger.warning("Clinic availability lookup unavailable")
            return "Clinic availability is temporarily unavailable. Please contact reception."

        if not slots:
            return f"No open appointment slots found for {srv.name} starting from {start_date}."
        lines = []
        for slot in slots[:5]:
            local = slot.start_at.astimezone(tz)
            formatted = local.strftime("%A, %d %B at %I:%M %p").replace(" 0", " ")
            lines.append(f"- {formatted} (ISO: {slot.start_at.isoformat()})")
        return f"Available slots for {srv.name}:\n" + "\n".join(lines)

    @llm.function_tool()
    async def create_booking_proposal(
        self,
        ctx: RunContext[Any],
        service_id: int,
        start_at: str,
        caller_name: str,
        caller_phone: str,
        caller_email: str | None = None,
    ) -> str:
        """Create a new booking proposal and play canonical readback."""
        try:
            res = await self.service.create_booking_proposal(
                voice_session_id=self.voice_session_id,
                service_id=service_id,
                start_at=start_at,
                caller_name=caller_name,
                caller_phone=caller_phone,
                caller_email=caller_email,
                created_turn_id=self._turn_counter - 1,
            )
        except SlotUnavailableError as exc:
            _mark_failure(exc)
            return (
                f"That appointment slot is no longer available: {exc}. Please select another time."
            )
        except SessionHandedOffError:
            _mark_trace("failed", "operation_failed")
            return "This call has been transferred to reception staff. Scheduling is disabled."
        except IdempotencyKeyConsumedError:
            _mark_trace("failed", "operation_failed")
            return "A proposal was already made for this request. Please respond to the readback or ask to start again."
        except SQLAlchemyError:
            _mark_trace("failed", "database_unavailable")
            logger.error("Database error creating booking proposal")
            return "I encountered an error preparing your booking. Please try again or contact reception."
        except Exception as exc:
            _mark_failure(exc)
            logger.exception("Error creating booking proposal")
            return "I encountered an error preparing your booking. Please try again or contact reception."

        readback_text = res["readback_text"]
        cid = UUID(res["challenge_id"])
        self._pending_proposal = ("booking", str(UUID(res["proposal_id"])))
        self._readback_task = None

        # Capture the last caller turn, not the next unused turn number. The
        # callback can run after another caller turn has already been recorded.
        readback_turn_id = self._turn_counter - 1
        # Playout single-spoken canonical readback
        handle = ctx.session.say(readback_text, allow_interruptions=True)

        def on_speech_done(h: Any) -> None:
            if not h.interrupted and h.exception() is None:
                self._readback_task = asyncio.create_task(
                    self.service.arm_readback(
                        challenge_id=cid, completed_turn_id=readback_turn_id
                    )
                )
            else:
                self._readback_task = asyncio.create_task(
                    self.service.invalidate_readback(
                        challenge_id=cid,
                        reason="barge_in_interrupted" if h.interrupted else "playback_error",
                    )
                )

        handle.add_done_callback(on_speech_done)
        raise llm.StopResponse()

    @llm.function_tool()
    async def confirm_booking(self, ctx: RunContext[Any], proposal_id: str = "") -> str:
        """Confirm a pending booking proposal following caller affirmative response."""
        if self._pending_proposal is None or self._pending_proposal[0] != "booking":
            _mark_trace("failed", "operation_failed")
            return "There is no booking awaiting confirmation. Please request a booking first."
        proposal_id = self._pending_proposal[1]
        try:
            correlated = resolve_causal_parent_user_turn(
                ctx.session.history.items,
                ctx.function_call.call_id,
                self._turn_tracker,
            )
        except Exception as exc:
            _mark_trace("failed", "missing_caller_turn")
            return f"Cannot verify confirmation: {exc}"

        try:
            if self._readback_task is not None:
                await self._readback_task
            res = await self.service.confirm_proposal(
                proposal_id_str=proposal_id,
                voice_session_id=self.voice_session_id,
                affirmative_turn_id=correlated.turn_id,
                affirmative_item_id=correlated.item_id,
                affirmative_transcript=correlated.transcript,
                speech_started_at=correlated.speech_started_at,
                is_cancellation=False,
            )
            _mark_trace("confirmed")
            return (
                f"Your appointment has been successfully confirmed! "
                f"Your appointment ID is {res['appointment_id']}. We look forward to seeing you."
            )
        except SlotUnavailableError as exc:
            _mark_failure(exc)
            return "That appointment slot is no longer available. Please choose another time."
        except (EarlySpeechBoundaryError, InvalidConsentError, ProposalExpiredError) as exc:
            _mark_failure(exc)
            return f"I could not confirm your booking: {exc}. Would you like to try again?"
        except SessionHandedOffError:
            _mark_trace("failed", "operation_failed")
            return "This call has been transferred to reception staff. Scheduling is disabled."
        except Exception as exc:
            _mark_failure(exc)
            logger.exception("Error confirming booking")
            return f"I encountered an error confirming your booking: {exc}"

    @llm.function_tool()
    async def lookup_appointment(
        self,
        ctx: RunContext[Any],
        phone_last4: str,
        caller_name: str,
        appointment_date: str | None = None,
    ) -> str:
        """Look up upcoming appointment using phone suffix and caller name with date disambiguation."""
        res = await self.service.lookup_appointment(
            voice_session_id=self.voice_session_id,
            client_id=self.client_id,
            phone_last4=phone_last4,
            caller_name=caller_name,
            appointment_date=appointment_date,
        )
        return str(res["message"])

    @llm.function_tool()
    async def verify_appointment_step_up(
        self,
        ctx: RunContext[Any],
        full_phone: str,
        caller_name: str,
    ) -> str:
        """Step-up identity verification requiring full phone number and caller name."""
        res = await self.service.verify_appointment_step_up(
            voice_session_id=self.voice_session_id,
            client_id=self.client_id,
            full_phone=full_phone,
            caller_name=caller_name,
        )
        return str(res["message"])

    @llm.function_tool()
    async def create_reschedule_proposal(
        self,
        ctx: RunContext[Any],
        new_start_at: str,
    ) -> str:
        """(IDOR-free) Create a proposal to reschedule verified appointment to new_start_at."""
        try:
            res = await self.service.create_reschedule_proposal(
                voice_session_id=self.voice_session_id,
                new_start_at=new_start_at,
                created_turn_id=self._turn_counter - 1,
            )
        except UnverifiedCallerSessionError as exc:
            _mark_failure(exc)
            return f"Please verify your appointment first: {exc}"
        except SlotUnavailableError as exc:
            _mark_failure(exc)
            return f"That slot is unavailable: {exc}. Please choose another time."
        except SessionHandedOffError:
            _mark_trace("failed", "operation_failed")
            return "This call has been transferred to reception staff. Scheduling is disabled."
        except Exception as exc:
            _mark_failure(exc)
            logger.exception("Error creating reschedule proposal")
            return f"I could not prepare the rescheduling request: {exc}"

        readback_text = res["readback_text"]
        cid = UUID(res["challenge_id"])
        self._pending_proposal = ("reschedule", str(UUID(res["proposal_id"])))
        self._readback_task = None

        handle = ctx.session.say(readback_text, allow_interruptions=True)

        readback_turn_id = self._turn_counter - 1

        def on_speech_done(h: Any) -> None:
            if not h.interrupted and h.exception() is None:
                self._readback_task = asyncio.create_task(
                    self.service.arm_readback(
                        challenge_id=cid, completed_turn_id=readback_turn_id
                    )
                )
            else:
                self._readback_task = asyncio.create_task(
                    self.service.invalidate_readback(
                        challenge_id=cid,
                        reason="barge_in_interrupted" if h.interrupted else "playback_error",
                    )
                )

        handle.add_done_callback(on_speech_done)
        raise llm.StopResponse()

    @llm.function_tool()
    async def confirm_reschedule(self, ctx: RunContext[Any], proposal_id: str = "") -> str:
        """Confirm a pending reschedule proposal following caller affirmative response."""
        if self._pending_proposal is None or self._pending_proposal[0] != "reschedule":
            _mark_trace("failed", "operation_failed")
            return "There is no reschedule awaiting confirmation. Please request a reschedule first."
        proposal_id = self._pending_proposal[1]
        try:
            correlated = resolve_causal_parent_user_turn(
                ctx.session.history.items,
                ctx.function_call.call_id,
                self._turn_tracker,
            )
        except Exception as exc:
            _mark_trace("failed", "missing_caller_turn")
            return f"Cannot verify confirmation: {exc}"

        try:
            if self._readback_task is not None:
                await self._readback_task
            res = await self.service.confirm_proposal(
                proposal_id_str=proposal_id,
                voice_session_id=self.voice_session_id,
                affirmative_turn_id=correlated.turn_id,
                affirmative_item_id=correlated.item_id,
                affirmative_transcript=correlated.transcript,
                speech_started_at=correlated.speech_started_at,
                is_cancellation=False,
            )
            _mark_trace("confirmed")
            return (
                f"Your appointment has been successfully rescheduled! "
                f"Your appointment ID is {res['appointment_id']}."
            )
        except (
            EarlySpeechBoundaryError,
            InvalidConsentError,
            ProposalExpiredError,
            StaleAppointmentVersionError,
        ) as exc:
            _mark_failure(exc)
            return f"I could not confirm rescheduling: {exc}."
        except SessionHandedOffError:
            _mark_trace("failed", "operation_failed")
            return "This call has been transferred to reception staff. Scheduling is disabled."
        except Exception as exc:
            _mark_failure(exc)
            logger.exception("Error confirming reschedule")
            return f"I encountered an error confirming your reschedule: {exc}"

    @llm.function_tool()
    async def create_cancellation_proposal(
        self,
        ctx: RunContext[Any],
        reason_category: str = "other",
    ) -> str:
        """(IDOR-free) Create a proposal to cancel verified appointment with controlled reason category."""
        allowed_reasons = {
            "scheduling_conflict",
            "patient_request",
            "provider_unavailable",
            "no_show",
            "other",
        }
        if reason_category not in allowed_reasons:
            reason_category = "other"

        try:
            res = await self.service.create_cancellation_proposal(
                voice_session_id=self.voice_session_id,
                reason_category=reason_category,
                created_turn_id=self._turn_counter - 1,
            )
        except UnverifiedCallerSessionError as exc:
            _mark_failure(exc)
            return f"Please verify your appointment first: {exc}"
        except Exception as exc:
            _mark_failure(exc)
            logger.exception("Error creating cancellation proposal")
            return f"I could not prepare the cancellation request: {exc}"

        readback_text = res["readback_text"]
        cid = UUID(res["challenge_id"])
        self._pending_proposal = ("cancellation", str(UUID(res["proposal_id"])))
        self._readback_task = None

        handle = ctx.session.say(readback_text, allow_interruptions=True)

        readback_turn_id = self._turn_counter - 1

        def on_speech_done(h: Any) -> None:
            if not h.interrupted and h.exception() is None:
                self._readback_task = asyncio.create_task(
                    self.service.arm_readback(
                        challenge_id=cid, completed_turn_id=readback_turn_id
                    )
                )
            else:
                self._readback_task = asyncio.create_task(
                    self.service.invalidate_readback(
                        challenge_id=cid,
                        reason="barge_in_interrupted" if h.interrupted else "playback_error",
                    )
                )

        handle.add_done_callback(on_speech_done)
        raise llm.StopResponse()

    @llm.function_tool()
    async def confirm_cancellation(self, ctx: RunContext[Any], proposal_id: str = "") -> str:
        """Confirm a pending cancellation proposal following caller affirmative response."""
        if self._pending_proposal is None or self._pending_proposal[0] != "cancellation":
            _mark_trace("failed", "operation_failed")
            return "There is no cancellation awaiting confirmation. Please request a cancellation first."
        proposal_id = self._pending_proposal[1]
        try:
            correlated = resolve_causal_parent_user_turn(
                ctx.session.history.items,
                ctx.function_call.call_id,
                self._turn_tracker,
            )
        except Exception as exc:
            _mark_trace("failed", "missing_caller_turn")
            return f"Cannot verify confirmation: {exc}"

        try:
            if self._readback_task is not None:
                await self._readback_task
            res = await self.service.confirm_proposal(
                proposal_id_str=proposal_id,
                voice_session_id=self.voice_session_id,
                affirmative_turn_id=correlated.turn_id,
                affirmative_item_id=correlated.item_id,
                affirmative_transcript=correlated.transcript,
                speech_started_at=correlated.speech_started_at,
                is_cancellation=True,
            )
            _mark_trace("cancelled")
            return f"Your appointment ID {res['appointment_id']} has been successfully cancelled."
        except (EarlySpeechBoundaryError, InvalidConsentError, ProposalExpiredError) as exc:
            _mark_failure(exc)
            return f"I could not cancel your appointment: {exc}."
        except SessionHandedOffError:
            _mark_trace("failed", "operation_failed")
            return "This call has been transferred to reception staff. Scheduling is disabled."
        except Exception as exc:
            _mark_failure(exc)
            logger.exception("Error confirming cancellation")
            return f"I encountered an error confirming cancellation: {exc}"

    @llm.function_tool()
    async def request_human_handoff(
        self,
        ctx: RunContext[Any],
        category: str = "caller_requested",
    ) -> str:
        """Transfer caller to human reception staff under PRD §6.5 triggers."""
        if category not in DEMO_HANDOFF_SCRIPTS:
            category = "caller_requested"

        await self.service.request_human_handoff(
            voice_session_id=self.voice_session_id,
            category=category,
        )
        return DEMO_HANDOFF_SCRIPTS[category]

    @llm.function_tool()
    async def escalate_medical_emergency(self, ctx: RunContext[Any]) -> str:
        """Immediately escalate a medical emergency or urgent symptoms."""
        await self.service.request_human_handoff(
            voice_session_id=self.voice_session_id,
            category="medical_emergency",
        )
        return DEMO_HANDOFF_SCRIPTS["medical_emergency"]


def profile_id_or_1(ctx: ClinicContextDTO) -> int:
    """Helper to extract clinic profile ID."""
    try:
        return ctx.profile.id
    except AttributeError:
        return 1

