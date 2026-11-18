"""Unit tests verifying Phase 2 worker pipeline, Flux turn wiring, and error handling."""

import asyncio
import importlib
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import llm, stt, tts
from livekit.plugins import deepgram, elevenlabs

from app.core.worker_config import WorkerSettings
from app.schemas.dto import ClinicContextDTO, ClinicProfileDTO


def _make_settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-livekit-key",
        livekit_api_secret="test-livekit-secret",
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        livekit_agent_name="voxora-agent",
        deepgram_api_key="test-dg-key",
        google_api_key="test-google-key",
        elevenlabs_api_key="test-el-key",
        elevenlabs_voice_id="test-voice-id",
    )


class _FakeEmitterSession:
    """Minimal EventEmitter stand-in matching AgentSession.on/emit/say/shutdown."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Any], Any]]] = {}
        self.say = AsyncMock()
        self.shutdown = MagicMock()
        self.start = AsyncMock()

    def on(self, event_name: str) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        def _decorator(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
            self._handlers.setdefault(event_name, []).append(fn)
            return fn

        return _decorator

    def emit(self, event_name: str, event: Any) -> None:
        for handler in self._handlers.get(event_name, []):
            handler(event)


def test_worker_module_imports_cleanly_without_env_or_native_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit tests must import worker.py without API keys, silero, or livekit_ffi DLLs."""
    for key in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "DEEPGRAM_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    from livekit.rtc import _ffi_client

    ffi_mock = MagicMock(side_effect=AssertionError("livekit_ffi must not load on import"))
    monkeypatch.setattr(_ffi_client, "get_ffi_lib", ffi_mock)

    sys.modules.pop("worker", None)
    try:
        worker = importlib.import_module("worker")
        assert hasattr(worker, "entrypoint")
        assert hasattr(worker, "build_agent_session")
        assert not hasattr(worker, "silero")
        ffi_mock.assert_not_called()
    finally:
        sys.modules.pop("worker", None)


def test_real_stt_v2_and_tts_plugin_instantiation() -> None:
    """Verify pinned plugins accept Flux STTv2 and ElevenLabs Flash v2.5 models."""
    stt_plugin = deepgram.STTv2(api_key="dummy-dg-key", model="flux-general-en")
    tts_plugin = elevenlabs.TTS(
        api_key="dummy-el-key",
        voice_id="dummy-voice-id",
        model="eleven_flash_v2_5",
    )
    assert stt_plugin._opts.model == "flux-general-en"
    assert tts_plugin._opts.model == "eleven_flash_v2_5"


@pytest.mark.asyncio
async def test_worker_entrypoint_configures_flux_vad_none_and_connect_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify entrypoint uses STTv2, Gemini, ElevenLabs, vad=None, stt turns, and greeting."""
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    try:
        settings = _make_settings()
        monkeypatch.setattr(worker, "get_worker_settings", lambda: settings)

        mock_sttv2_instance = MagicMock(name="sttv2_instance")
        mock_llm_instance = MagicMock(name="llm_instance")
        mock_tts_instance = MagicMock(name="tts_instance")

        mock_sttv2_cls = MagicMock(return_value=mock_sttv2_instance)
        mock_legacy_stt_cls = MagicMock()
        mock_llm_cls = MagicMock(return_value=mock_llm_instance)
        mock_tts_cls = MagicMock(return_value=mock_tts_instance)

        fake_session = _FakeEmitterSession()
        mock_agent_session_cls = MagicMock(return_value=fake_session)

        monkeypatch.setattr(worker.deepgram, "STTv2", mock_sttv2_cls)
        monkeypatch.setattr(worker.deepgram, "STT", mock_legacy_stt_cls)
        monkeypatch.setattr(worker.google, "LLM", mock_llm_cls)
        monkeypatch.setattr(worker.elevenlabs, "TTS", mock_tts_cls)
        monkeypatch.setattr(worker, "AgentSession", mock_agent_session_cls)

        mock_clinic_ctx = ClinicContextDTO(
            profile=ClinicProfileDTO(
                id=1,
                slug="brightsmile",
                name="BrightSmile Dental Clinic",
                description="Large clinic description " * 250,
                address_line1="24 Green Park",
                address_line2=None,
                city="New Delhi",
                state="Delhi",
                postal_code="110016",
                country="India",
                phone=None,
                email=None,
                website=None,
                timezone="Asia/Kolkata",
            ),
            hours=[],
            closures=[],
            services=[],
            policies=[],
            faqs=[],
            local_now=datetime.now(UTC),
        )
        monkeypatch.setattr(worker, "create_async_engine", lambda *a, **k: AsyncMock())
        monkeypatch.setattr(worker, "load_clinic_context", AsyncMock(return_value=mock_clinic_ctx))
        monkeypatch.setattr(worker, "receptionist_enabled", AsyncMock(return_value=True))

        ctx = MagicMock()
        ctx.room.name = "voxora-a1b2c3d4e5f6"
        ctx.job.id = "job-test-001"
        ctx.connect = AsyncMock()

        await worker.entrypoint(ctx)

        ctx.connect.assert_awaited_once()
        mock_legacy_stt_cls.assert_not_called()
        mock_sttv2_cls.assert_called_once_with(
            api_key="test-dg-key",
            model="flux-general-en",
        )
        mock_llm_cls.assert_called_once_with(
            api_key="test-google-key",
            model="gemini-3.1-flash-lite",
        )
        mock_tts_cls.assert_called_once_with(
            api_key="test-el-key",
            voice_id="test-voice-id",
            model="eleven_flash_v2_5",
        )

        mock_agent_session_cls.assert_called_once()
        kwargs = mock_agent_session_cls.call_args.kwargs
        assert kwargs["stt"] is mock_sttv2_instance
        assert kwargs["llm"] is mock_llm_instance
        assert kwargs["tts"] is mock_tts_instance
        assert kwargs["vad"] is None
        assert kwargs["turn_handling"] == {"turn_detection": "stt"}
        assert kwargs["user_away_timeout"] is None
        assert kwargs["conn_options"] == settings.build_session_connect_options()

        fake_session.start.assert_awaited_once()
        assert fake_session.start.call_args.kwargs["record"] is False
        agent_arg = fake_session.start.call_args.kwargs["agent"]
        assert "Asia/Kolkata" in agent_arg.instructions
        assert "get_clinic_info" in agent_arg.instructions
        assert "Large clinic description" not in agent_arg.instructions
        fake_session.say.assert_awaited_once_with(
            worker.DETERMINISTIC_GREETING,
            allow_interruptions=True,
        )

        # Clean up any inactivity task scheduled by mark_session_ready().
        fake_session.emit("close", SimpleNamespace(reason=SimpleNamespace(value="test_done")))
        await asyncio.sleep(0)
    finally:
        sys.modules.pop("worker", None)


@pytest.mark.asyncio
async def test_coordinator_flux_turn_events_drive_inactivity_and_redact_transcripts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify Flux-mapped events reset inactivity without logging raw transcript text."""
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    try:
        settings = _make_settings()
        session = _FakeEmitterSession()
        coordinator = worker.VoiceSessionCoordinator(
            session=session,
            settings=settings,
            room_name="voxora-a1b2c3d4e5f6",
            session_id="sess-0123456789ab",
        )
        coordinator.attach()
        coordinator.inactivity.mark_session_ready()

        secret_phrase = "My name is Priya phone 9876543210"
        with caplog.at_level("INFO", logger="voxora.worker"):
            session.emit(
                "user_state_changed",
                SimpleNamespace(old_state="listening", new_state="speaking"),
            )
            session.emit(
                "user_input_transcribed",
                SimpleNamespace(transcript=secret_phrase, is_final=False),
            )
            session.emit(
                "user_state_changed",
                SimpleNamespace(old_state="speaking", new_state="listening"),
            )
            assert coordinator.inactivity.timer_mode == "handoff_grace"

            session.emit(
                "agent_state_changed",
                SimpleNamespace(old_state="listening", new_state="thinking"),
            )
            assert coordinator.inactivity.timer_mode == "none"

        joined_logs = "\n".join(r.getMessage() for r in caplog.records)
        assert "Priya" not in joined_logs
        assert "9876543210" not in joined_logs

        await coordinator.inactivity.aclose()
    finally:
        sys.modules.pop("worker", None)


@pytest.mark.asyncio
async def test_recoverable_provider_error_does_not_terminate_or_duplicate() -> None:
    """Recoverable STT/LLM/TTS errors log a retry event without stopping the session."""
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    try:
        session = _FakeEmitterSession()
        coordinator = worker.VoiceSessionCoordinator(
            session=session,
            settings=_make_settings(),
            room_name="voxora-a1b2c3d4e5f6",
            session_id="sess-0123456789ab",
        )
        coordinator.attach()

        session.emit(
            "error",
            SimpleNamespace(
                error=stt.STTError(
                    timestamp=1.0,
                    label="deepgram.STTv2",
                    error=TimeoutError("transient"),
                    recoverable=True,
                ),
                source=None,
            ),
        )
        await asyncio.sleep(0)

        assert not coordinator.inactivity.closed
        session.say.assert_not_awaited()
        session.shutdown.assert_not_called()
    finally:
        sys.modules.pop("worker", None)


@pytest.mark.asyncio
async def test_unrecoverable_llm_error_before_audio_speaks_once_and_closes() -> None:
    """Pre-audio unrecoverable LLM error speaks one apology and closes gracefully."""
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    try:
        session = _FakeEmitterSession()
        coordinator = worker.VoiceSessionCoordinator(
            session=session,
            settings=_make_settings(),
            room_name="voxora-a1b2c3d4e5f6",
            session_id="sess-0123456789ab",
        )
        coordinator.attach()

        session.emit(
            "error",
            SimpleNamespace(
                error=llm.LLMError(
                    timestamp=1.0,
                    label="google.LLM",
                    error=RuntimeError("503"),
                    recoverable=False,
                ),
                source=None,
            ),
        )
        await asyncio.sleep(0)

        session.say.assert_awaited_once_with(
            worker.PROVIDER_ERROR_GOODBYE_TEXT,
            allow_interruptions=False,
        )
        session.shutdown.assert_called_once_with(drain=True)
    finally:
        sys.modules.pop("worker", None)


@pytest.mark.asyncio
async def test_unrecoverable_llm_error_after_audio_started_does_not_duplicate_speech() -> None:
    """If assistant audio already began for the turn, never speak a competing apology."""
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    try:
        session = _FakeEmitterSession()
        coordinator = worker.VoiceSessionCoordinator(
            session=session,
            settings=_make_settings(),
            room_name="voxora-a1b2c3d4e5f6",
            session_id="sess-0123456789ab",
        )
        coordinator.attach()
        session.emit(
            "agent_state_changed",
            SimpleNamespace(old_state="thinking", new_state="speaking"),
        )

        session.emit(
            "error",
            SimpleNamespace(
                error=llm.LLMError(
                    timestamp=1.0,
                    label="google.LLM",
                    error=RuntimeError("mid-stream drop"),
                    recoverable=False,
                ),
                source=None,
            ),
        )
        await asyncio.sleep(0)

        session.say.assert_not_awaited()
        session.shutdown.assert_called_once_with(drain=False)
    finally:
        sys.modules.pop("worker", None)


@pytest.mark.asyncio
async def test_unrecoverable_tts_error_closes_without_calling_say() -> None:
    """Unrecoverable TTS error never loops into session.say() and shuts down cleanly."""
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    try:
        session = _FakeEmitterSession()
        coordinator = worker.VoiceSessionCoordinator(
            session=session,
            settings=_make_settings(),
            room_name="voxora-a1b2c3d4e5f6",
            session_id="sess-0123456789ab",
        )
        coordinator.attach()

        session.emit(
            "error",
            SimpleNamespace(
                error=tts.TTSError(
                    timestamp=1.0,
                    label="elevenlabs.TTS",
                    error=ConnectionError("ws closed"),
                    recoverable=False,
                ),
                source=None,
            ),
        )
        await asyncio.sleep(0)

        session.say.assert_not_awaited()
        session.shutdown.assert_called_once_with(drain=False)
    finally:
        sys.modules.pop("worker", None)


def test_create_worker_server_registers_effective_agent_name() -> None:
    """Verify AgentServer registers under effective dispatch name cleanly."""
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    try:
        server = worker.create_worker_server(_make_settings())
        assert getattr(server, "_agent_name", None) == "voxora-agent"
        assert server._num_idle_processes == 0
        assert server._host == "127.0.0.1"
    finally:
        sys.modules.pop("worker", None)


def test_verify_livekit_native_rtc_fails_fast_on_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify worker startup check exits cleanly when livekit_ffi cannot load."""
    sys.modules.pop("worker", None)
    worker = importlib.import_module("worker")
    try:
        from livekit.rtc import _ffi_client

        def _raise_blocked() -> Any:
            raise OSError("[WinError 4551] An Application Control policy has blocked this file")

        monkeypatch.setattr(_ffi_client, "get_ffi_lib", _raise_blocked)
        with pytest.raises(SystemExit) as exc_info:
            worker.verify_livekit_native_rtc()
        assert exc_info.value.code == 1
    finally:
        sys.modules.pop("worker", None)


@pytest.mark.asyncio
async def test_worker_disposes_engine_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify worker entrypoint always disposes engine even if context loading fails."""
    worker = importlib.import_module("worker")
    monkeypatch.setattr(worker, "receptionist_enabled", AsyncMock(return_value=True))
    mock_settings = _make_settings()
    monkeypatch.setattr(worker, "get_worker_settings", lambda: mock_settings)

    mock_engine = AsyncMock()
    monkeypatch.setattr(worker, "create_async_engine", lambda *args, **kwargs: mock_engine)

    # Make load_clinic_context raise an error
    monkeypatch.setattr(
        worker,
        "load_clinic_context",
        AsyncMock(side_effect=RuntimeError("Neon connection refused")),
    )

    mock_ctx = MagicMock()
    mock_ctx.connect = AsyncMock()
    mock_ctx.room = MagicMock()
    mock_ctx.room.name = "voxora-room-1234"

    with pytest.raises(RuntimeError, match="Neon connection refused"):
        await worker.entrypoint(mock_ctx)

    mock_engine.dispose.assert_awaited_once()
