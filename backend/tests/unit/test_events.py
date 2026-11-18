"""Unit tests for redacted structured voice event logging and error classification."""

import json
import logging
from types import SimpleNamespace

import pytest
from livekit.agents import llm, stt, tts

from app.voice.events import (
    build_voice_event_payload,
    classify_provider_error,
    generate_opaque_session_id,
    log_voice_event,
    redact_event_metadata,
    sanitize_opaque_identifier,
)


def test_opaque_identifiers_pass_and_non_opaque_ids_are_hashed() -> None:
    assert sanitize_opaque_identifier("voxora-a1b2c3d4e5f6", prefix="room") == "voxora-a1b2c3d4e5f6"
    assert sanitize_opaque_identifier("sess-0123456789ab", prefix="sess") == "sess-0123456789ab"
    assert sanitize_opaque_identifier("RM_AbCdEf12345", prefix="room") == "RM_AbCdEf12345"

    hashed = sanitize_opaque_identifier("Priya-Sharma-9876543210", prefix="room")
    assert hashed.startswith("room-hash-")
    assert "Priya" not in hashed
    assert "9876543210" not in hashed
    assert sanitize_opaque_identifier(None, prefix="room") == "room-unknown"


def test_generate_opaque_session_id_matches_expected_format() -> None:
    sid = generate_opaque_session_id()
    assert sid.startswith("sess-")
    assert sanitize_opaque_identifier(sid, prefix="sess") == sid


def test_redact_event_metadata_drops_transcripts_and_scrubs_pii() -> None:
    filtered = redact_event_metadata(
        {
            "transcript": "My phone number is 9876543210",
            "text": "hello doctor",
            "phone": "+91 9876543210",
            "email": "priya@example.com",
            "name": "Priya",
            "stage": "llm",
            "provider": "priya@example.com",
            "reason": "Call 98765 43210",
            "e2e_latency_ms": 420,
        }
    )

    assert "transcript" not in filtered
    assert "text" not in filtered
    assert "phone" not in filtered
    assert "email" not in filtered
    assert "name" not in filtered
    assert filtered["stage"] == "llm"
    assert filtered["provider"] == "[REDACTED]"
    assert filtered["reason"] == "[REDACTED]"
    assert filtered["e2e_latency_ms"] == 420


def test_log_voice_event_emits_valid_json_without_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_logger = logging.getLogger("voxora.test.events")
    with caplog.at_level(logging.INFO, logger="voxora.test.events"):
        emitted = log_voice_event(
            test_logger,
            "session_started",
            room_name="Alice-9999999999",
            session_id="sess-0123456789ab",
            metadata={"transcript": "secret speech", "stage": "stt"},
        )

    assert emitted["room"].startswith("room-hash-")
    assert "Alice" not in emitted["room"]
    assert "9999999999" not in emitted["room"]
    assert "transcript" not in emitted.get("meta", {})
    assert emitted["meta"] == {"stage": "stt"}

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert msg.startswith("[VOICE_EVENT] ")
    parsed = json.loads(msg.removeprefix("[VOICE_EVENT] "))
    assert parsed["event"] == "session_started"


def test_build_voice_event_payload_serializes_cleanly() -> None:
    payload = build_voice_event_payload(
        "inactivity_nudge_sent",
        room_name="voxora-001122334455",
        session_id="sess-aabbccddeeff",
    )
    assert payload["room"] == "voxora-001122334455"
    assert payload["session_id"] == "sess-aabbccddeeff"
    assert payload["event"] == "inactivity_nudge_sent"
    assert "meta" not in payload


def test_classify_provider_error_identifies_stt_llm_tts_and_unknown() -> None:
    stt_ev = SimpleNamespace(
        error=stt.STTError(
            timestamp=1.0,
            label="deepgram.STTv2",
            error=TimeoutError("ws timeout"),
            recoverable=True,
        ),
        source=None,
    )
    llm_ev = SimpleNamespace(
        error=llm.LLMError(
            timestamp=1.0, label="google.LLM", error=RuntimeError("500"), recoverable=False
        ),
        source=None,
    )
    tts_ev = SimpleNamespace(
        error=tts.TTSError(
            timestamp=1.0, label="elevenlabs.TTS", error=ConnectionError("drop"), recoverable=False
        ),
        source=None,
    )
    unknown_ev = SimpleNamespace(error=RuntimeError("boom"), source=None)

    stt_info = classify_provider_error(stt_ev)
    assert stt_info.stage == "stt"
    assert stt_info.recoverable is True
    assert stt_info.error_type == "TimeoutError"

    llm_info = classify_provider_error(llm_ev)
    assert llm_info.stage == "llm"
    assert llm_info.recoverable is False
    assert llm_info.error_type == "RuntimeError"

    tts_info = classify_provider_error(tts_ev)
    assert tts_info.stage == "tts"
    assert tts_info.recoverable is False
    assert tts_info.error_type == "ConnectionError"

    unknown_info = classify_provider_error(unknown_ev)
    assert unknown_info.stage == "unknown"
    assert unknown_info.recoverable is False
