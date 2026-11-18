"""Unit tests verifying session signing secret configuration and validation."""

import pytest
from pydantic import ValidationError

from app.core.worker_config import WorkerSettings


def _base_kwargs() -> dict[str, str]:
    return {
        "livekit_url": "wss://test.livekit.cloud",
        "livekit_api_key": "test-key",
        "livekit_api_secret": "test-secret",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "deepgram_api_key": "test-dg-key",
        "google_api_key": "test-google-key",
        "elevenlabs_api_key": "test-el-key",
        "elevenlabs_voice_id": "test-voice-id",
    }


def test_default_session_signing_secret_is_valid() -> None:
    """Default signing secret is at least 32 characters long."""
    settings = WorkerSettings(_env_file=None, **_base_kwargs())
    assert len(settings.session_signing_secret) >= 32
    assert settings.session_signing_secret == "voxora_default_session_signing_secret_key_32_chars"


def test_custom_session_signing_secret_valid() -> None:
    """A valid 32+ character custom secret is accepted."""
    custom_secret = "a_very_secure_secret_with_more_than_32_characters"
    settings = WorkerSettings(
        _env_file=None,
        session_signing_secret=custom_secret,
        **_base_kwargs(),
    )
    assert settings.session_signing_secret == custom_secret


def test_short_session_signing_secret_raises_validation_error() -> None:
    """Signing secret with fewer than 32 characters is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        WorkerSettings(
            _env_file=None,
            session_signing_secret="too_short_secret",
            **_base_kwargs(),
        )
    assert "session_signing_secret must be at least 32 characters long" in str(exc_info.value)


def test_empty_or_whitespace_session_signing_secret_rejected() -> None:
    """Empty or whitespace-only signing secret is rejected."""
    with pytest.raises(ValidationError):
        WorkerSettings(
            _env_file=None,
            session_signing_secret="   ",
            **_base_kwargs(),
        )


def test_previous_signing_secrets_validation() -> None:
    """Each secret in previous_session_signing_secrets must be >= 32 characters."""
    valid_list = [
        "old_secret_one_with_32_or_more_characters!",
        "old_secret_two_with_32_or_more_characters!",
    ]
    settings = WorkerSettings(
        _env_file=None,
        previous_session_signing_secrets=valid_list,
        **_base_kwargs(),
    )
    assert settings.previous_session_signing_secrets == valid_list

    with pytest.raises(ValidationError) as exc_info:
        WorkerSettings(
            _env_file=None,
            previous_session_signing_secrets=["short_old_secret"],
            **_base_kwargs(),
        )
    assert "previous_session_signing_secrets must be at least 32 characters long" in str(
        exc_info.value
    )
