from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_asyncpg_url(url: str) -> str:
    """Normalize PostgreSQL URLs to use asyncpg and asyncpg-compatible TLS options."""
    stripped = url.strip()
    for prefix in (
        "postgres://",
        "postgresql://",
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
    ):
        if stripped.startswith(prefix):
            stripped = "postgresql+asyncpg://" + stripped[len(prefix) :]
            break

    if not stripped.startswith("postgresql+asyncpg://"):
        return stripped

    parsed = urlsplit(stripped)
    if not parsed.query:
        return stripped

    raw_params = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(k in {"sslmode", "channel_binding"} for k, _ in raw_params):
        return stripped

    has_ssl = any(k == "ssl" for k, _ in raw_params)
    filtered_params: list[tuple[str, str]] = []
    for k, v in raw_params:
        if k == "channel_binding":
            continue
        if k == "sslmode":
            if not has_ssl and v:
                filtered_params.append(("ssl", v))
                has_ssl = True
            continue
        filtered_params.append((k, v))

    new_query = urlencode(filtered_params)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))


class AppSettings(BaseSettings):
    """
    Application-level configuration for Voxora API service.

    ENVIRONMENT defaults to 'production' to enforce a strict fail-closed boundary
    for development-only unauthenticated endpoints.
    """

    # Environment & Security
    environment: str = Field(
        default="production",
        description=(
            "Runtime environment ('development', 'staging', 'production'). "
            "Defaults to 'production'."
        ),
    )
    database_url: str = Field(
        description="PostgreSQL asyncpg connection URL",
    )
    test_database_url: str | None = Field(
        default=None,
        description="Optional isolated test database URL",
    )
    allowed_origins: list[str] | str = Field(
        default=["http://localhost:3000", "http://127.0.0.1:3000"],
        description="Allowed CORS origins",
    )
    db_echo: bool = Field(
        default=False,
        description="Echo SQL statements to stdout",
    )
    trace_access_secret: str | None = Field(default=None, repr=False)
    owner_jwt_secret: str | None = Field(default=None, repr=False)
    google_calendar_client_id: str | None = None
    google_calendar_client_secret: str | None = Field(default=None, repr=False)
    google_calendar_redirect_uri: str | None = None
    google_calendar_encryption_key: str | None = Field(default=None, repr=False)

    # LiveKit Cloud
    livekit_url: str = Field(
        description="LiveKit Cloud WebSocket URL (wss://...)",
    )
    livekit_api_key: str = Field(
        description="LiveKit Cloud API Key",
    )
    livekit_api_secret: str = Field(
        description="LiveKit Cloud API Secret",
    )

    # Agent Dispatch Configuration
    livekit_agent_name: str = Field(
        default="voxora-agent",
        description="Default agent dispatch name",
    )
    livekit_agent_name_override: str | None = Field(
        default=None,
        description="Optional override taking precedence over LIVEKIT_AGENT_NAME",
    )

    # Consent & Session Cryptography
    session_signing_secret: str = Field(
        default="voxora_default_session_signing_secret_key_32_chars",
        description="Secret key for signing HMAC consent tokens and server authorizations",
    )

    @field_validator("database_url", "test_database_url", mode="after")
    @classmethod
    def normalize_database_urls(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return v
        return normalize_asyncpg_url(v)

    @field_validator("allowed_origins", mode="after")
    @classmethod
    def normalize_allowed_origins(cls, v: list[str] | str) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                import json

                try:
                    return list(json.loads(v))
                except Exception:
                    return [v]
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @property
    def effective_agent_name(self) -> str:
        """Returns the effective agent dispatch name, prioritizing LIVEKIT_AGENT_NAME_OVERRIDE."""
        return self.livekit_agent_name_override or self.livekit_agent_name

    @model_validator(mode="after")
    def validate_agent_name(self) -> "AppSettings":
        name = self.effective_agent_name
        if not name or not name.strip():
            raise ValueError("Effective agent dispatch name cannot be empty or whitespace.")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()  # type: ignore[call-arg]
