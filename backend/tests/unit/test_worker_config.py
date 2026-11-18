import pytest
from pydantic import ValidationError

from app.core.config import AppSettings
from app.core.worker_config import WorkerSettings


def make_valid_worker_kwargs() -> dict[str, str]:
    return {
        "livekit_url": "wss://test.livekit.cloud",
        "livekit_api_key": "test-livekit-key",
        "livekit_api_secret": "test-livekit-secret",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "deepgram_api_key": "test-deepgram-key",
        "google_api_key": "test-google-key",
        "elevenlabs_api_key": "test-elevenlabs-key",
        "elevenlabs_voice_id": "test-voice-id",
    }


def test_worker_settings_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_AGENT_NAME", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    monkeypatch.delenv("DEEPGRAM_STT_MODEL", raising=False)
    monkeypatch.delenv("GOOGLE_LLM_MODEL", raising=False)
    monkeypatch.delenv("ELEVENLABS_TTS_MODEL", raising=False)
    ws = WorkerSettings(_env_file=None, **make_valid_worker_kwargs())
    assert ws.livekit_url == "wss://test.livekit.cloud"
    assert ws.livekit_agent_name == "voxora-agent"
    assert ws.effective_agent_name == "voxora-agent"
    assert ws.deepgram_stt_model == "flux-general-en"
    assert ws.google_llm_model == "gemini-3.1-flash-lite"
    assert ws.elevenlabs_tts_model == "eleven_flash_v2_5"


def test_worker_settings_default_agent_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_AGENT_NAME", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    ws = WorkerSettings(_env_file=None, **make_valid_worker_kwargs())
    assert ws.livekit_agent_name == "voxora-agent"
    assert ws.effective_agent_name == "voxora-agent"


def test_worker_settings_agent_name_override_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEKIT_AGENT_NAME_OVERRIDE", "custom-override")
    ws = WorkerSettings(_env_file=None, **make_valid_worker_kwargs())
    assert ws.effective_agent_name == "custom-override"


def test_worker_settings_matches_app_settings_agent_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_AGENT_NAME", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    ws = WorkerSettings(_env_file=None, **make_valid_worker_kwargs())
    app_settings = AppSettings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-livekit-key",
        livekit_api_secret="test-livekit-secret",
    )
    assert ws.effective_agent_name == app_settings.effective_agent_name == "voxora-agent"


def test_worker_settings_missing_google_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = make_valid_worker_kwargs()
    del kwargs["google_api_key"]
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, **kwargs)


def test_worker_settings_accepts_gemini_api_key_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = make_valid_worker_kwargs()
    del kwargs["google_api_key"]
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "alias-gemini-key")
    ws = WorkerSettings(_env_file=None, **kwargs)
    assert ws.google_api_key == "alias-gemini-key"

    monkeypatch.setenv("GOOGLE_API_KEY", "primary-google-key")
    ws_primary = WorkerSettings(_env_file=None, **kwargs)
    assert ws_primary.google_api_key == "primary-google-key"


def test_worker_settings_missing_deepgram_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = make_valid_worker_kwargs()
    del kwargs["deepgram_api_key"]
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, **kwargs)


def test_worker_settings_missing_elevenlabs_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = make_valid_worker_kwargs()
    del kwargs["elevenlabs_api_key"]
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, **kwargs)


def test_worker_settings_includes_database_url() -> None:
    ws = WorkerSettings(_env_file=None, **make_valid_worker_kwargs())
    assert ws is not None
    assert hasattr(ws, "database_url")
    assert ws.database_url.startswith("postgresql+asyncpg://")


def test_worker_settings_empty_agent_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = make_valid_worker_kwargs()
    kwargs["livekit_agent_name"] = ""
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, **kwargs)


def test_worker_settings_whitespace_agent_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = make_valid_worker_kwargs()
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, livekit_agent_name="   ", **kwargs)
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, livekit_agent_name_override="   ", **kwargs)


def test_worker_settings_model_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPGRAM_STT_MODEL", "flux-general-multi")
    monkeypatch.setenv("GOOGLE_LLM_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2")
    ws = WorkerSettings(_env_file=None, **make_valid_worker_kwargs())
    assert ws.deepgram_stt_model == "flux-general-multi"
    assert ws.google_llm_model == "gemini-3.5-flash"
    assert ws.elevenlabs_tts_model == "eleven_flash_v2"


@pytest.mark.parametrize(
    "field",
    ["deepgram_stt_model", "google_llm_model", "elevenlabs_tts_model"],
)
def test_worker_settings_blank_model_raises(field: str) -> None:
    kwargs = make_valid_worker_kwargs()
    kwargs[field] = "   "
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, **kwargs)


def test_worker_settings_phase2_defaults_and_session_connect_options() -> None:
    ws = WorkerSettings(_env_file=None, **make_valid_worker_kwargs())
    assert ws.clinic_timezone == "Asia/Kolkata"
    assert ws.inactivity_nudge_timeout_s == 10.0
    assert ws.inactivity_disconnect_timeout_s == 20.0
    assert ws.turn_handoff_grace_s == 1.5

    conn_opts = ws.build_session_connect_options()
    assert conn_opts.stt_conn_options.timeout == ws.stt_connect_timeout_s
    assert conn_opts.stt_conn_options.liveness_timeout == ws.stt_liveness_timeout_s
    assert conn_opts.stt_conn_options.max_retry == ws.stt_max_retry
    assert conn_opts.llm_conn_options.timeout == ws.llm_timeout_s
    assert conn_opts.llm_conn_options.max_retry == ws.llm_max_retry
    assert conn_opts.tts_conn_options.timeout == ws.tts_timeout_s
    assert conn_opts.tts_conn_options.max_retry == ws.tts_max_retry
    assert conn_opts.max_unrecoverable_errors == ws.max_unrecoverable_errors


def test_worker_settings_rejects_invalid_timezone_or_inactivity_bounds() -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, clinic_timezone="Invalid/Zone", **make_valid_worker_kwargs())

    with pytest.raises(ValidationError):
        WorkerSettings(
            _env_file=None,
            inactivity_nudge_timeout_s=5.0,
            turn_handoff_grace_s=5.0,
            **make_valid_worker_kwargs(),
        )

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, llm_timeout_s=0.0, **make_valid_worker_kwargs())

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, tts_max_retry=-1, **make_valid_worker_kwargs())


def test_worker_settings_database_url_validation() -> None:
    kwargs = make_valid_worker_kwargs()
    kwargs["database_url"] = "postgres://user:pass@host/db?sslmode=require"
    ws = WorkerSettings(_env_file=None, **kwargs)
    assert ws.database_url.startswith("postgresql+asyncpg://")

    kwargs["database_url"] = "   "
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, **kwargs)
