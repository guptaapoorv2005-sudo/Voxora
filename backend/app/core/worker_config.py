"""Worker configuration and runtime defaults for the Voxora voice agent.

Think of this file like a `config/env.js` module validated with `zod` in Node.js:
`pydantic-settings` reads environment variables (from the process environment or
`.env`) at runtime, validates their types and bounds, and refuses to start the
worker if required secrets are missing or numeric limits are invalid.

This class is the **single source of truth** for Phase 2 worker behavior:
model IDs, clinic timezone, inactivity timing, and stage-specific provider
timeouts/retries.
"""

from __future__ import annotations

from livekit.agents import APIConnectOptions, SessionConnectOptions
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import normalize_asyncpg_url
from app.voice.context import resolve_clinic_timezone


class WorkerSettings(BaseSettings):
    """Dedicated worker configuration for the LiveKit voice agent process.

    Independent of FastAPI and ``DATABASE_URL`` so the real-time voice worker
    can start and scale without requiring web-API settings, while Phases 3+ will
    add explicit database access for clinic configuration and bookings.
    """

    livekit_url: str = Field(description="LiveKit Cloud WebSocket URL")
    livekit_api_key: str = Field(description="LiveKit Cloud API Key")
    livekit_api_secret: str = Field(description="LiveKit Cloud API Secret")

    database_url: str = Field(
        description="PostgreSQL asyncpg connection URL for loading clinic context",
    )

    deepgram_api_key: str = Field(description="Deepgram API Key for STT")
    deepgram_stt_model: str = Field(
        default="flux-general-en",
        description="Deepgram STT v2 (Flux) model ID",
    )
    google_api_key: str = Field(
        validation_alias=AliasChoices(
            "google_api_key",
            "GOOGLE_API_KEY",
            "gemini_api_key",
            "GEMINI_API_KEY",
        ),
        description="Google AI Studio API Key for Gemini LLM",
    )
    google_llm_model: str = Field(
        default="gemini-3.1-flash-lite",
        description="Google Gemini LLM model ID",
    )
    elevenlabs_api_key: str = Field(description="ElevenLabs API Key for TTS")
    elevenlabs_voice_id: str = Field(description="ElevenLabs Voice ID")
    elevenlabs_tts_model: str = Field(
        default="eleven_flash_v2_5",
        description="ElevenLabs TTS model ID",
    )

    livekit_agent_name: str = Field(
        default="voxora-agent",
        description="Default agent dispatch name",
    )
    livekit_agent_name_override: str | None = Field(
        default=None,
        description="Optional override taking precedence over LIVEKIT_AGENT_NAME",
    )

    # Clinic orientation context (Phase 3 will fetch full clinic config from Neon).
    clinic_timezone: str = Field(
        default="Asia/Kolkata",
        description="Clinic IANA timezone identifier injected into each session",
    )

    # HMAC Session Signing Secret for Two-Phase Voice Authorization (Phase 5)
    session_signing_secret: str = Field(
        default="voxora_default_session_signing_secret_key_32_chars",
        description="HMAC secret used to sign two-phase Session A/B voice authorization tokens (min 32 chars)",
    )
    previous_session_signing_secrets: list[str] = Field(
        default_factory=list,
        description="Optional list of previously active signing secrets for zero-downtime rotation",
    )

    # Application-level call inactivity policy (seconds).
    inactivity_nudge_timeout_s: float = Field(
        default=10.0,
        gt=0.0,
        description="Idle caller silence duration before speaking one polite nudge",
    )
    inactivity_disconnect_timeout_s: float = Field(
        default=20.0,
        gt=0.0,
        description="Additional silence after nudge playout finishes before closing session",
    )
    turn_handoff_grace_s: float = Field(
        default=1.5,
        gt=0.0,
        description=(
            "Settling window after EndOfTurn before treating idle silence as inactivity, "
            "preventing false timer arms while LiveKit transitions agent_state to thinking"
        ),
    )

    # Stage-specific provider timeouts and bounded retries (applied via LiveKit SDK).
    stt_connect_timeout_s: float = Field(
        default=10.0,
        gt=0.0,
        description="Deepgram STTv2 WebSocket connection timeout in seconds",
    )
    stt_liveness_timeout_s: float = Field(
        default=6.0,
        gt=0.0,
        description="Deepgram WebSocket liveness probe timeout in seconds",
    )
    stt_max_retry: int = Field(
        default=2,
        ge=0,
        description="Bounded reconnect retries for Deepgram STT WebSocket drops",
    )
    llm_timeout_s: float = Field(
        default=10.0,
        gt=0.0,
        description="Gemini LLM HTTP/stream timeout in seconds",
    )
    llm_max_retry: int = Field(
        default=1,
        ge=0,
        description="Bounded pre-response retry count for transient Gemini errors",
    )
    tts_timeout_s: float = Field(
        default=10.0,
        gt=0.0,
        description="ElevenLabs TTS connection/synthesis timeout in seconds",
    )
    tts_max_retry: int = Field(
        default=1,
        ge=0,
        description="Bounded pre-audio retry count for ElevenLabs TTS (only when 0 audio sent)",
    )
    provider_retry_interval_s: float = Field(
        default=1.0,
        ge=0.0,
        description="Delay in seconds before retrying a recoverable stage error",
    )
    max_unrecoverable_errors: int = Field(
        default=1,
        ge=1,
        description="Maximum consecutive unrecoverable errors per provider before closing session",
    )

    @property
    def effective_agent_name(self) -> str:
        """Return the effective agent dispatch name, honoring LIVEKIT_AGENT_NAME_OVERRIDE."""
        return self.livekit_agent_name_override or self.livekit_agent_name

    def build_session_connect_options(self) -> SessionConnectOptions:
        """Build per-stage LiveKit ``APIConnectOptions`` from validated worker settings.

        LiveKit applies these independently to STT, LLM, and TTS so transient errors
        can retry safely *before* partial output/audio reaches the caller, without
        wrapping or replaying the whole streaming turn.
        """
        return SessionConnectOptions(
            stt_conn_options=APIConnectOptions(
                max_retry=self.stt_max_retry,
                retry_interval=self.provider_retry_interval_s,
                timeout=self.stt_connect_timeout_s,
                liveness_timeout=self.stt_liveness_timeout_s,
            ),
            llm_conn_options=APIConnectOptions(
                max_retry=self.llm_max_retry,
                retry_interval=self.provider_retry_interval_s,
                timeout=self.llm_timeout_s,
                liveness_timeout=self.stt_liveness_timeout_s,
            ),
            tts_conn_options=APIConnectOptions(
                max_retry=self.tts_max_retry,
                retry_interval=self.provider_retry_interval_s,
                timeout=self.tts_timeout_s,
                liveness_timeout=self.stt_liveness_timeout_s,
            ),
            max_unrecoverable_errors=self.max_unrecoverable_errors,
        )

    @field_validator("database_url", mode="after")
    @classmethod
    def normalize_db_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("database_url cannot be empty or whitespace.")
        return normalize_asyncpg_url(v)

    @field_validator("session_signing_secret", mode="after")
    @classmethod
    def validate_session_signing_secret(cls, v: str) -> str:
        if not v or len(v.strip()) < 32:
            raise ValueError("session_signing_secret must be at least 32 characters long.")
        return v

    @field_validator("previous_session_signing_secrets", mode="after")
    @classmethod
    def validate_previous_session_signing_secrets(cls, v: list[str]) -> list[str]:
        for secret in v:
            if not secret or len(secret.strip()) < 32:
                raise ValueError(
                    "All previous_session_signing_secrets must be at least 32 characters long."
                )
        return v

    @model_validator(mode="after")
    def validate_settings(self) -> WorkerSettings:
        """Validate cross-field invariants after individual fields are parsed."""
        name = self.effective_agent_name
        if not name or not name.strip():
            raise ValueError("Effective agent dispatch name cannot be empty or whitespace.")

        for field_name, value in (
            ("deepgram_stt_model", self.deepgram_stt_model),
            ("google_llm_model", self.google_llm_model),
            ("elevenlabs_tts_model", self.elevenlabs_tts_model),
        ):
            if not value or not value.strip():
                raise ValueError(f"{field_name} cannot be empty or whitespace.")

        resolve_clinic_timezone(self.clinic_timezone)

        if self.turn_handoff_grace_s >= self.inactivity_nudge_timeout_s:
            raise ValueError(
                "turn_handoff_grace_s must be strictly smaller than inactivity_nudge_timeout_s."
            )

        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
