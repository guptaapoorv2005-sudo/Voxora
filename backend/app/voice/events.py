"""Structured, privacy-preserving voice event logging and provider error classification.

In a Node.js/Express app, you might use Winston or Pino to log JSON lines while
redacting `req.headers.authorization` and `req.body.phone`. Voice AI has a similar
requirement with even stricter boundaries:

1. **No caller transcripts or PII in operational logs:** Speech text, caller names,
   phone numbers, and emails must never appear in Phase 2 structured voice logs.
2. **Opaque room and session identifiers:** LiveKit rooms created by our FastAPI
   endpoint follow ``voxora-<12 hex chars>`` and worker sessions use ``sess-<12 hex chars>``.
   If an external/manual test room ever arrives with a non-opaque name (which could
   accidentally include a person's name or phone number), we replace it with a
   one-way SHA-256 tag before logging.
3. **Reliable LiveKit error classification:** LiveKit Agents emits ``ErrorEvent``
   objects carrying typed payloads (``STTError``, ``LLMError``, ``TTSError``) plus
   a ``source`` component. We inspect those typed fields so the worker knows which
   pipeline stage failed without leaking raw exception messages or URLs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from livekit.agents import llm, stt, tts

VoiceEventType = Literal[
    "session_started",
    "session_ended",
    "user_turn_started",
    "user_turn_ended",
    "user_transcript_received",
    "agent_state_changed",
    "inactivity_nudge_sent",
    "inactivity_disconnect",
    "provider_retry",
    "provider_error",
    "turn_metrics_recorded",
    "session_usage_recorded",
]

ErrorStage = Literal["stt", "llm", "tts", "realtime_llm", "unknown"]

# Strict opaque ID patterns allowed verbatim in logs (zero caller PII).
_OPAQUE_ID_PATTERNS = (
    re.compile(r"^voxora-[0-9a-f]{12}$"),
    re.compile(r"^sess-[0-9a-f]{12}$"),
    re.compile(r"^(?:AJ|RM)_[A-Za-z0-9]{4,32}$"),
)

# Defense-in-depth regexes to catch email addresses or phone-like digit sequences.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_LIKE_RE = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")

# Only explicitly allowlisted scalar fields may appear in structured voice event logs.
_ALLOWED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "stage",
        "provider",
        "error_type",
        "recoverable",
        "reason",
        "old_state",
        "new_state",
        "word_count",
        "e2e_latency_ms",
        "transcription_delay_ms",
        "llm_ttft_ms",
        "tts_ttfb_ms",
        "prompt_tokens",
        "completion_tokens",
        "tts_characters",
        "turn_audio_started",
        "is_final",
        "trigger",
    }
)


@dataclass(frozen=True)
class ProviderErrorInfo:
    """Normalized stage classification extracted from a LiveKit ``ErrorEvent``."""

    stage: ErrorStage
    recoverable: bool
    error_type: str
    provider: str


def generate_opaque_session_id() -> str:
    """Create a fresh server-generated opaque session identifier (zero PII)."""
    return f"sess-{uuid4().hex[:12]}"


def mask_phone_number(phone: str) -> str:
    """Mask phone number to show only the last 4 digits (e.g. '***-***-1234')."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "****"
    return f"***-***-{digits[-4:]}"


def sanitize_opaque_identifier(value: str | None, *, prefix: str) -> str:
    """Return ``value`` if it matches an opaque ID format, otherwise hash it one-way.

    This guarantees that even if a room is manually created outside FastAPI with a
    name like ``"Priya-9876543210"``, no personal data is ever written to logs.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return f"{prefix}-unknown"

    if any(pattern.fullmatch(cleaned) for pattern in _OPAQUE_ID_PATTERNS):
        return cleaned

    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-hash-{digest}"


def _scrub_scalar(value: Any) -> str | int | float | bool | None:
    """Normalize metadata values to safe primitives and scrub accidental PII strings."""
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value).strip()
    if _EMAIL_RE.search(text) or _PHONE_LIKE_RE.search(text):
        return "[REDACTED]"
    # Cap arbitrary label/enum lengths so unexpected payloads stay bounded.
    return text[:64]


def redact_event_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Filter event metadata to the safe scalar allowlist and scrub string values."""
    if not metadata:
        return {}

    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in _ALLOWED_METADATA_KEYS:
            continue
        cleaned[key] = _scrub_scalar(value)
    return cleaned


def classify_provider_error(event: Any) -> ProviderErrorInfo:
    """Identify which voice stage (STT, LLM, or TTS) produced a LiveKit ``ErrorEvent``.

    In ``livekit-agents==1.8.11``, ``ErrorEvent`` carries:
      - ``error``: ``STTError`` / ``LLMError`` / ``TTSError`` / ``RealtimeModelError``
      - ``source``: the plugin instance (``stt.STT``, ``llm.LLM``, ``tts.TTS``, etc.)

    Checking ``event.error`` first (both class and ``.type`` discriminator) and
    falling back to ``event.source`` ensures reliable stage classification in both
    live sessions and mocked unit tests.
    """
    err = getattr(event, "error", event)
    source = getattr(event, "source", None)
    err_type_tag = getattr(err, "type", None)

    stage: ErrorStage = "unknown"
    if isinstance(err, stt.STTError) or err_type_tag == "stt_error" or isinstance(source, stt.STT):
        stage = "stt"
    elif (
        isinstance(err, llm.LLMError) or err_type_tag == "llm_error" or isinstance(source, llm.LLM)
    ):
        stage = "llm"
    elif (
        isinstance(err, tts.TTSError) or err_type_tag == "tts_error" or isinstance(source, tts.TTS)
    ):
        stage = "tts"
    elif isinstance(err, llm.RealtimeModelError) or err_type_tag == "realtime_model_error":
        stage = "realtime_llm"

    recoverable = bool(getattr(err, "recoverable", False))
    inner_exc = getattr(err, "error", None)
    if inner_exc is not None:
        error_type = type(inner_exc).__name__
    elif err is not None:
        error_type = type(err).__name__
    else:
        error_type = "UnknownError"

    provider_raw = getattr(err, "label", None) or getattr(source, "provider", None) or stage
    provider = str(_scrub_scalar(provider_raw) or stage)

    return ProviderErrorInfo(
        stage=stage,
        recoverable=recoverable,
        error_type=error_type,
        provider=provider,
    )


def build_voice_event_payload(
    event_type: VoiceEventType,
    *,
    room_name: str | None,
    session_id: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted, JSON-serializable dictionary for a structured voice event."""
    payload: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_type,
        "room": sanitize_opaque_identifier(room_name, prefix="room"),
        "session_id": sanitize_opaque_identifier(session_id, prefix="sess"),
    }
    safe_meta = redact_event_metadata(metadata)
    if safe_meta:
        payload["meta"] = safe_meta
    return payload


def log_voice_event(
    logger_instance: logging.Logger,
    event_type: VoiceEventType,
    *,
    room_name: str | None,
    session_id: str | None,
    metadata: Mapping[str, Any] | None = None,
    level: int = logging.INFO,
) -> dict[str, Any]:
    """Emit a structured JSON voice event log line and return the emitted payload."""
    payload = build_voice_event_payload(
        event_type,
        room_name=room_name,
        session_id=session_id,
        metadata=metadata,
    )
    logger_instance.log(level, "[VOICE_EVENT] %s", json.dumps(payload, sort_keys=True))
    return payload
