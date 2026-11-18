"""Voxora LiveKit Agent Worker — Phase 2 Conversation Behaviour and Latency.

Think of this file like a dedicated real-time worker server (separate from our
Express-like FastAPI HTTP server in ``app/main.py``):

- FastAPI handles HTTP requests (health checks, short-lived LiveKit tokens).
- ``worker.py`` connects out to LiveKit Cloud, waits for a caller to enter a
  room, and runs a streaming **Deepgram STTv2 (Flux) -> Gemini LLM -> ElevenLabs TTS**
  voice session inside ``entrypoint(ctx)``.

Key Phase 2 architecture choices implemented here:
1. **Import-safe startup:** Settings, native ``livekit_ffi``, and provider clients
   are initialized lazily instead of at module import time so unit tests can
   import ``worker`` safely on Windows without loading unavailable DLLs.
2. **Deepgram Flux owns turn detection and barge-in:** ``vad=None`` is passed
   explicitly alongside ``turn_handling={"turn_detection": "stt"}`` so LiveKit
   does not silently instantiate Silero VAD.
3. **Race-free inactivity policy:** ``user_away_timeout=None`` disables LiveKit's
   15s default timer so :class:`CallInactivityController` enforces Voxora's exact
   10s nudge -> 20s additional silence -> graceful termination lifecycle.
4. **Stage-specific timeouts and graceful termination:** ``SessionConnectOptions``
   applies bounded retries per stage (where LiveKit guards against replaying
   already-emitted audio/chunks), and unrecoverable ``ErrorEvent`` callbacks
   terminate the session gracefully without duplicating speech.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    AgentStateChangedEvent,
    CloseEvent,
    ConversationItemAddedEvent,
    ErrorEvent,
    JobContext,
    SessionUsageUpdatedEvent,
    UserInputTranscribedEvent,
    UserStateChangedEvent,
    cli,
)
from livekit.agents.llm import ChatMessage
from livekit.plugins import deepgram, elevenlabs, google
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.worker_config import WorkerSettings
from app.services.clinic_context import load_clinic_context
from app.services.voice_booking_service import VoiceBookingService
from app.voice.clinic_tools import ClinicVoiceTools
from app.voice.context import build_session_context
from app.voice.events import (
    ProviderErrorInfo,
    classify_provider_error,
    generate_opaque_session_id,
    log_voice_event,
)
from app.voice.inactivity import CallInactivityController
from app.voice.prompts import (
    DETERMINISTIC_GREETING,
    PROVIDER_ERROR_GOODBYE_TEXT,
    build_system_instructions,
)
from app.voice.tracing import CallTraceRecorder
from app.voice.tracing_agent import TracingAgent

logger = logging.getLogger("voxora.worker")


def get_worker_settings() -> WorkerSettings:
    """Load `.env` if present and validate ``WorkerSettings`` at runtime."""
    load_dotenv()
    return WorkerSettings()  # type: ignore[call-arg]


def build_agent_session(settings: WorkerSettings) -> AgentSession[Any]:
    """Construct the Phase 2 LiveKit ``AgentSession`` from validated settings."""
    return AgentSession(
        stt=deepgram.STTv2(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_stt_model,
        ),
        llm=google.LLM(
            api_key=settings.google_api_key,
            model=settings.google_llm_model,
        ),
        tts=elevenlabs.TTS(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.elevenlabs_voice_id,
            model=settings.elevenlabs_tts_model,
        ),
        # Explicitly pass vad=None: omitting vad causes AgentSession to auto-load
        # Silero VAD. Deepgram Flux STTv2 owns turn detection and barge-in here.
        vad=None,
        turn_handling={"turn_detection": "stt"},
        # Disable LiveKit's built-in 15s user-away timer so Voxora's 10s + 20s
        # CallInactivityController owns the call inactivity policy cleanly.
        user_away_timeout=None,
        conn_options=settings.build_session_connect_options(),
    )


async def _speak_safely(session: Any, text: str, *, allow_interruptions: bool) -> None:
    """Invoke ``session.say`` and await playout if awaitable, suppressing teardown errors."""
    handle = session.say(text, allow_interruptions=allow_interruptions)
    if inspect.isawaitable(handle):
        await handle


class VoiceSessionCoordinator:
    """Coordinates turn events, inactivity timers, metrics, and graceful error termination."""

    def __init__(
        self,
        *,
        session: Any,
        settings: WorkerSettings,
        room_name: str | None,
        session_id: str,
        clinic_tools: ClinicVoiceTools | None = None,
        trace: CallTraceRecorder | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.room_name = room_name
        self.session_id = session_id
        self.clinic_tools = clinic_tools
        self.trace = trace

        self.turn_audio_started = False
        self.fallback_spoken = False
        self._error_tasks: set[asyncio.Task[None]] = set()

        self.inactivity = CallInactivityController(
            session=session,
            room_name=room_name,
            session_id=session_id,
            nudge_timeout_s=settings.inactivity_nudge_timeout_s,
            disconnect_timeout_s=settings.inactivity_disconnect_timeout_s,
            turn_handoff_grace_s=settings.turn_handoff_grace_s,
            logger_instance=logger,
        )

    def _log(self, logger_instance: logging.Logger, event: Any, **kwargs: Any) -> dict[str, Any]:
        payload = log_voice_event(logger_instance, event, **kwargs)
        if self.trace is not None:
            self.trace.record(event, kwargs.get("metadata"))
        return payload

    def attach(self) -> None:
        """Subscribe synchronous event callbacks onto the LiveKit ``AgentSession``."""

        @self.session.on("user_state_changed")
        def on_user_state_changed(event: UserStateChangedEvent) -> None:
            if event.new_state == "speaking":
                self.turn_audio_started = False
                self._log(
                    logger,
                    "user_turn_started",
                    room_name=self.room_name,
                    session_id=self.session_id,
                    metadata={"old_state": event.old_state, "new_state": event.new_state},
                )
            elif event.new_state == "listening":
                self._log(
                    logger,
                    "user_turn_ended",
                    room_name=self.room_name,
                    session_id=self.session_id,
                    metadata={"old_state": event.old_state, "new_state": event.new_state},
                )
            self.inactivity.on_user_state_changed(event.new_state)

        @self.session.on("agent_state_changed")
        def on_agent_state_changed(event: AgentStateChangedEvent) -> None:
            if event.new_state == "speaking":
                self.turn_audio_started = True
            elif event.new_state == "listening":
                self.turn_audio_started = False

            self._log(
                logger,
                "agent_state_changed",
                room_name=self.room_name,
                session_id=self.session_id,
                metadata={"old_state": event.old_state, "new_state": event.new_state},
            )
            self.inactivity.on_agent_state_changed(event.new_state)

        @self.session.on("user_input_transcribed")
        def on_user_input_transcribed(event: UserInputTranscribedEvent) -> None:
            transcript = getattr(event, "transcript", "") or ""
            is_final = bool(getattr(event, "is_final", False))
            if transcript.strip():
                # Log only structural boolean metadata — never raw speech text.
                self._log(
                    logger,
                    "user_transcript_received",
                    room_name=self.room_name,
                    session_id=self.session_id,
                    metadata={"is_final": is_final},
                )
            self.inactivity.on_user_input_transcribed(transcript, is_final=is_final)

        @self.session.on("conversation_item_added")
        def on_conversation_item_added(event: ConversationItemAddedEvent) -> None:
            item = event.item
            if not isinstance(item, ChatMessage):
                return

            raw_metrics = getattr(item, "metrics", None)
            metrics_dict = (
                dict(raw_metrics) if raw_metrics and hasattr(raw_metrics, "items") else {}
            )

            if item.role == "user":
                if self.clinic_tools is not None and getattr(item, "id", None):
                    self.clinic_tools.record_turn(item.id)
                tx_delay = metrics_dict.get("transcription_delay")
                tx_delay_ms = int(tx_delay * 1000) if tx_delay is not None else None
                self._log(
                    logger,
                    "turn_metrics_recorded",
                    room_name=self.room_name,
                    session_id=self.session_id,
                    metadata={
                        "stage": "stt",
                        "transcription_delay_ms": tx_delay_ms,
                    },
                )
            elif item.role == "assistant":
                e2e = metrics_dict.get("e2e_latency")
                llm_ttft = metrics_dict.get("llm_node_ttft")
                tts_ttfb = metrics_dict.get("tts_node_ttfb")
                word_count = len(item.content.split()) if isinstance(item.content, str) else 0

                self._log(
                    logger,
                    "turn_metrics_recorded",
                    room_name=self.room_name,
                    session_id=self.session_id,
                    metadata={
                        "stage": "assistant_turn",
                        "word_count": word_count,
                        "e2e_latency_ms": int(e2e * 1000) if e2e is not None else None,
                        "llm_ttft_ms": int(llm_ttft * 1000) if llm_ttft is not None else None,
                        "tts_ttfb_ms": int(tts_ttfb * 1000) if tts_ttfb is not None else None,
                    },
                )

        @self.session.on("session_usage_updated")
        def on_session_usage_updated(event: SessionUsageUpdatedEvent) -> None:
            usage = event.usage
            self._log(
                logger,
                "session_usage_recorded",
                room_name=self.room_name,
                session_id=self.session_id,
                metadata={
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "tts_characters": getattr(usage, "tts_characters", None),
                },
            )

        @self.session.on("error")
        def on_error(event: ErrorEvent) -> None:
            info = classify_provider_error(event)
            if info.recoverable:
                self._log(
                    logger,
                    "provider_retry",
                    room_name=self.room_name,
                    session_id=self.session_id,
                    metadata={
                        "stage": info.stage,
                        "provider": info.provider,
                        "error_type": info.error_type,
                        "recoverable": True,
                    },
                    level=logging.WARNING,
                )
                return

            self._log(
                logger,
                "provider_error",
                room_name=self.room_name,
                session_id=self.session_id,
                metadata={
                    "stage": info.stage,
                    "provider": info.provider,
                    "error_type": info.error_type,
                    "recoverable": False,
                    "turn_audio_started": self.turn_audio_started,
                },
                level=logging.ERROR,
            )
            task = asyncio.create_task(
                self.handle_unrecoverable_error(info),
                name="voxora-unrecoverable-error-shutdown",
            )
            self._error_tasks.add(task)
            task.add_done_callback(self._error_tasks.discard)

        @self.session.on("close")
        def on_close(event: CloseEvent) -> None:
            reason = getattr(getattr(event, "reason", None), "value", "unknown")
            self._log(
                logger,
                "session_ended",
                room_name=self.room_name,
                session_id=self.session_id,
                metadata={"reason": str(reason)},
            )
            # Flush in-memory conversation history on room disconnect
            try:
                if hasattr(self.session, "conversation") and hasattr(
                    self.session.conversation, "truncate"
                ):
                    self.session.conversation.truncate(max_items=0)
            except Exception:
                pass
            task = asyncio.create_task(
                self.inactivity.aclose(),
                name="voxora-inactivity-close",
            )
            self._error_tasks.add(task)
            task.add_done_callback(self._error_tasks.discard)

    async def handle_unrecoverable_error(self, info: ProviderErrorInfo) -> None:
        """Gracefully end the session on unrecoverable errors without duplicating audio."""
        if self.inactivity.closed:
            return

        # Only speak an apology when TTS itself did not fail AND no partial audio for
        # this assistant turn has already started playing to the caller.
        if (
            info.stage in {"stt", "llm"}
            and not self.turn_audio_started
            and not self.fallback_spoken
        ):
            self.fallback_spoken = True
            try:
                await _speak_safely(
                    self.session,
                    PROVIDER_ERROR_GOODBYE_TEXT,
                    allow_interruptions=False,
                )
            except Exception as exc:
                logger.warning(
                    "Fallback goodbye speech failed (%s); closing session.",
                    type(exc).__name__,
                )
            await self.inactivity.trigger_shutdown(drain=True)
            return

        await self.inactivity.trigger_shutdown(drain=False)


_retention_task: asyncio.Task[None] | None = None


async def _periodic_retention_cleanup_task(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Background task running on worker startup and every 24 hours under advisory locks."""
    is_initial_run = True
    while True:
        if not is_initial_run:
            await asyncio.sleep(86400)  # Sleep 24 hours between scheduled runs
        is_initial_run = False

        try:
            async with sessionmaker() as session:
                # 1. Advisory Lock Gating: Prevent duplicate concurrent runs across workers
                lock_res = await session.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtext('retention_cleanup'), 1);")
                )
                if not lock_res.scalar():
                    logger.info("Retention cleanup already running on another worker; skipping.")
                    continue

                # 2. Rate-Limiting Records (24-hour TTL threshold -> <= 48h physical disk retention)
                await session.execute(
                    text(
                        "DELETE FROM client_verification_attempts WHERE attempted_at < NOW() - INTERVAL '24 hours';"
                    )
                )
                await session.execute(
                    text(
                        "DELETE FROM appointment_verification_failures WHERE attempted_at < NOW() - INTERVAL '24 hours';"
                    )
                )
                await session.execute(
                    text(
                        "DELETE FROM token_issuance_attempts WHERE attempted_at < NOW() - INTERVAL '24 hours';"
                    )
                )

                # 3. Expired Caller Verification Sessions (Inactive for > 24 hours -> <= 48h physical disk retention)
                await session.execute(
                    text(
                        "DELETE FROM caller_verification_sessions WHERE updated_at < NOW() - INTERVAL '24 hours';"
                    )
                )

                # 4. Proposal PII Redaction (> 30 days old -> <= 31d physical disk retention)
                await session.execute(
                    text("""
                        UPDATE booking_proposals 
                        SET caller_name = 'REDACTED', caller_phone = 'REDACTED', caller_email = NULL 
                        WHERE created_at < NOW() - INTERVAL '30 days' AND caller_phone != 'REDACTED';
                    """)
                )
                await session.execute(
                    text("""
                        UPDATE cancellation_proposals 
                        SET caller_name = 'REDACTED', caller_phone = 'REDACTED' 
                        WHERE created_at < NOW() - INTERVAL '30 days' AND caller_phone != 'REDACTED';
                    """)
                )

                # Keep future appointments usable; anonymize only after their end.
                await session.execute(text("""
                    UPDATE appointments SET caller_name = 'REDACTED',
                        caller_phone = 'REDACTED', caller_email = NULL,
                        cancellation_reason = NULL
                    WHERE end_at < NOW() - INTERVAL '30 days'
                      AND caller_phone != 'REDACTED'
                """))
                # Events cascade with their parent call. Calls still in progress are
                # retained until they end so the active recorder is not disrupted.
                await session.execute(text("""
                    DELETE FROM calls WHERE ended_at < NOW() - INTERVAL '30 days'
                """))

                await session.commit()
                logger.info("Completed retention cleanup cycle.")
        except asyncio.CancelledError:
            logger.info("Retention cleanup task cancelled on worker shutdown.")
            break
        except Exception:
            logger.exception(
                "Failed to run periodic retention cleanup; scheduling retry in 5 minutes."
            )
            await asyncio.sleep(300)  # Fast retry on database failure


async def receptionist_enabled(session: AsyncSession) -> bool:
    from sqlalchemy import select

    from app.db.models.clinic import BusinessProfile

    return (await session.scalar(
        select(BusinessProfile.receptionist_enabled).where(BusinessProfile.slug == "brightsmile")
    )) is True


async def entrypoint(ctx: JobContext) -> None:
    """Entrypoint invoked by LiveKit when a caller joins a room and triggers dispatch."""
    global _retention_task
    settings = get_worker_settings()
    session_id = generate_opaque_session_id()
    room_name = getattr(getattr(ctx, "room", None), "name", None)

    log_voice_event(
        logger,
        "session_started",
        room_name=room_name,
        session_id=session_id,
    )

    await ctx.connect()

    # Database engine and session factory for clinic context & Phase 5 voice booking tools
    engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    trace = CallTraceRecorder(session_factory, room_name, session_id)
    trace.record("session_started")
    trace_task = asyncio.create_task(trace.run(), name="voxora-trace-flush")

    async def _on_shutdown(reason: str = "") -> None:
        try:
            async with session_factory() as shutdown_session:
                await shutdown_session.execute(
                    text(
                        "UPDATE consent_challenges "
                        "SET status = 'invalidated', invalidated_at = NOW(), invalidation_reason = 'worker_shutdown' "
                        "WHERE session_id = :sid AND status = 'pending_readback';"
                    ),
                    {"sid": session_id},
                )
                await shutdown_session.commit()
        except Exception:
            pass
        finally:
            trace.stop()
            await trace_task
            await trace.flush(outcome=trace.outcome)
            await engine.dispose()

    ctx.add_shutdown_callback(_on_shutdown)

    # Start background retention cleanup task if not already running
    if _retention_task is None or _retention_task.done():
        _retention_task = asyncio.create_task(
            _periodic_retention_cleanup_task(session_factory),
            name="voxora-retention-cleanup",
        )

    try:
        async with session_factory() as db_session:
            if not await receptionist_enabled(db_session):
                ctx.shutdown(reason="receptionist disabled")
                return
            clinic_ctx = await load_clinic_context(db_session, slug="brightsmile")
    except Exception:
        await engine.dispose()
        raise

    # Determine client_id from remote participant identity if available
    remote_participants = getattr(getattr(ctx, "room", None), "remote_participants", {})
    caller_p = next(iter(remote_participants.values()), None) if remote_participants else None
    client_id = getattr(caller_p, "identity", None) or room_name or session_id

    booking_service = VoiceBookingService(
        session_factory=session_factory,
        session_signing_secret=settings.session_signing_secret,
    )
    clinic_tools = ClinicVoiceTools(
        service=booking_service,
        session_factory=session_factory,
        clinic_ctx=clinic_ctx,
        voice_session_id=session_id,
        client_id=client_id,
        trace=trace,
    )

    session = build_agent_session(settings)
    coordinator = VoiceSessionCoordinator(
        session=session,
        settings=settings,
        room_name=room_name,
        session_id=session_id,
        clinic_tools=clinic_tools,
        trace=trace,
    )
    coordinator.attach()

    session_context = build_session_context(clinic_ctx.profile.timezone)
    instructions = build_system_instructions(session_context=session_context)

    await session.start(
        room=ctx.room,
        agent=TracingAgent(trace=trace, instructions=instructions, tools=clinic_tools.get_tools()),
        record=False,
    )
    await _speak_safely(session, DETERMINISTIC_GREETING, allow_interruptions=True)
    coordinator.inactivity.mark_session_ready()


def verify_livekit_native_rtc() -> None:
    """Verify the native ``livekit-rtc`` FFI library can load before registering the worker."""
    try:
        from livekit.rtc._ffi_client import get_ffi_lib

        get_ffi_lib()
    except (ImportError, OSError) as exc:
        logger.error(
            "Unable to load livekit-rtc native library (livekit_ffi): %s. "
            "On Windows, Smart App Control / Application Control may block unsigned PyPI "
            "DLLs (WinError 4551), preventing the worker from connecting to rooms.",
            exc,
        )
        raise SystemExit(1) from exc


def create_worker_server(settings: WorkerSettings) -> AgentServer:
    """Create and verify an ``AgentServer`` bound to ``settings.effective_agent_name``."""
    effective_agent_name = settings.effective_agent_name
    os.environ["LIVEKIT_AGENT_NAME"] = effective_agent_name

    # On a small shared instance, start job processes only when a call arrives.
    # Keep LiveKit's internal health server local so Render exposes the API port.
    server = AgentServer(num_idle_processes=0, host="127.0.0.1")
    server.rtc_session(entrypoint, agent_name=effective_agent_name)

    registered_name = getattr(server, "_agent_name", None)
    if registered_name != effective_agent_name:
        raise RuntimeError(
            f"AgentServer registration mismatch: server registered as '{registered_name}', "
            f"expected '{effective_agent_name}'"
        )

    logger.info(
        "Worker configured | agent='%s' | stt=STTv2(%s) | llm=%s | tts=%s | "
        "turn_detection=stt | vad=None | tz=%s",
        registered_name,
        settings.deepgram_stt_model,
        settings.google_llm_model,
        settings.elevenlabs_tts_model,
        settings.clinic_timezone,
    )
    return server


def main() -> None:
    """CLI entrypoint for launching the LiveKit worker process."""
    if len(sys.argv) > 1 and sys.argv[1] in {"dev", "start", "connect"}:
        verify_livekit_native_rtc()

    settings = get_worker_settings()
    server = create_worker_server(settings)
    cli.run_app(server)


if __name__ == "__main__":
    main()

