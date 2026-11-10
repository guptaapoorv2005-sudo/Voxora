import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.delenv("DB_ECHO", raising=False)
    settings = Settings()
    assert settings.database_url == "postgresql+asyncpg://u:p@localhost:5432/db"
    assert settings.db_echo is False
    assert settings.environment == "development"


def test_settings_db_echo_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("DB_ECHO", "true")
    settings = Settings()
    assert settings.db_echo is True


def test_settings_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Ensure .env file doesn't supply it during this isolated unit test
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

