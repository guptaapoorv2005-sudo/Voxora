from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(
        description="Async SQLAlchemy database connection URL (asyncpg)"
    )
    test_database_url: str | None = Field(
        default=None,
        description="Optional isolated test database URL",
    )
    environment: str = Field(
        default="development",
        description="Application environment (development/production/test)",
    )
    allowed_origins: list[str] = Field(
        default=["http://localhost:3000"],
        description="CORS allowed origins",
    )
    db_echo: bool = Field(
        default=False,
        description="Enable SQLAlchemy query echo logging when debugging",
    )

    # Voice / Agent config placeholders for future phases
    livekit_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: str | None = None
    gemini_api_key: str | None = None
    deepgram_api_key: str | None = None
    elevenlabs_api_key: str | None = None

    # Auth placeholders for Phase 7
    jwt_secret: str | None = None
    cookie_secret: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
