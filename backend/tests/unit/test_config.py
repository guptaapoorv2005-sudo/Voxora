import pytest
from pydantic import ValidationError

from app.core.config import AppSettings


def test_app_settings_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    settings = AppSettings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    assert settings.database_url == "postgresql+asyncpg://u:p@localhost:5432/db"
    assert settings.livekit_url == "wss://test.livekit.cloud"
    assert settings.livekit_api_key == "test-api-key"
    assert settings.livekit_api_secret == "test-api-secret"
    assert settings.effective_agent_name == "voxora-agent"
    assert settings.environment == "production"


def test_app_settings_missing_livekit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            livekit_api_key="test-api-key",
            livekit_api_secret="test-api-secret",
        )


def test_app_settings_missing_livekit_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            livekit_url="wss://test.livekit.cloud",
            livekit_api_secret="test-api-secret",
        )


def test_app_settings_missing_livekit_api_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            livekit_url="wss://test.livekit.cloud",
            livekit_api_key="test-api-key",
        )


def test_app_settings_default_environment_is_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = AppSettings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    assert settings.environment == "production"


def test_app_settings_default_agent_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_AGENT_NAME", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    settings = AppSettings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    assert settings.livekit_agent_name == "voxora-agent"
    assert settings.effective_agent_name == "voxora-agent"


def test_app_settings_agent_name_override_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("LIVEKIT_AGENT_NAME_OVERRIDE", "custom-override")
    settings = AppSettings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    assert settings.effective_agent_name == "custom-override"


def test_app_settings_empty_agent_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            livekit_url="wss://test.livekit.cloud",
            livekit_api_key="test-api-key",
            livekit_api_secret="test-api-secret",
            livekit_agent_name="",
        )


def test_app_settings_whitespace_agent_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            livekit_url="wss://test.livekit.cloud",
            livekit_api_key="test-api-key",
            livekit_api_secret="test-api-secret",
            livekit_agent_name="   ",
        )
    with pytest.raises(ValidationError):
        AppSettings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            livekit_url="wss://test.livekit.cloud",
            livekit_api_key="test-api-key",
            livekit_api_secret="test-api-secret",
            livekit_agent_name_override="   ",
        )


def test_app_settings_normalizes_postgresql_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    settings = AppSettings(
        _env_file=None,
        database_url=(
            "postgresql://user:pass@ep-demo.neon.tech/neondb"
            "?sslmode=require&channel_binding=require"
        ),
        test_database_url="postgres://user:pass@localhost:5432/testdb?sslmode=disable",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    assert (
        settings.database_url
        == "postgresql+asyncpg://user:pass@ep-demo.neon.tech/neondb?ssl=require"
    )
    assert (
        settings.test_database_url
        == "postgresql+asyncpg://user:pass@localhost:5432/testdb?ssl=disable"
    )

    psycopg_settings = AppSettings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost:5432/db?ssl=require",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    assert psycopg_settings.database_url == "postgresql+asyncpg://u:p@localhost:5432/db?ssl=require"
