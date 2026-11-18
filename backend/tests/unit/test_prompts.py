"""Unit tests verifying Phase 3 voice prompt boundaries and spoken copy."""

from __future__ import annotations

import pytest

from app.voice.prompts import (
    DETERMINISTIC_GREETING,
    INACTIVITY_GOODBYE_TEXT,
    INACTIVITY_NUDGE_TEXT,
    PROVIDER_ERROR_GOODBYE_TEXT,
    build_system_instructions,
)


def test_deterministic_greeting_includes_ai_disclosure_and_clinic_name() -> None:
    lowered = DETERMINISTIC_GREETING.lower()
    assert "brightsmile dental clinic" in lowered
    assert "ai" in lowered
    assert "not a human" in lowered


def test_system_instructions_include_required_phase3_guardrails() -> None:
    session_ctx = (
        "Current clinic date and time (Asia/Kolkata): Thursday, 12 November 2026, 6:56 PM IST."
    )
    prompt = build_system_instructions(session_ctx)

    assert session_ctx in prompt
    assert "English" in prompt
    assert "never output markdown" in prompt
    # Phase 2 placeholder is removed
    assert "do NOT yet have access to the clinic database" not in prompt
    assert "Phase 2 Boundary" not in prompt
    # Phase 3 accurate facts rule is present
    assert "get_clinic_info" in prompt
    assert "list_services" in prompt
    assert "check_availability" in prompt
    assert "clarification" in prompt
    assert "Read back names, phone numbers" in prompt
    assert "One moment while I check that." in prompt
    assert "Never diagnose" in prompt
    assert "request_human_handoff" in prompt
    assert len(prompt) < 2500


def test_system_instructions_rejects_empty_contexts() -> None:
    with pytest.raises(ValueError, match="session_context cannot be empty"):
        build_system_instructions("   ")


def test_system_instructions_enforces_max_prompt_chars() -> None:
    valid_session = "Current date/time: Friday 10 AM."
    # Artificially low ceiling
    with pytest.raises(ValueError, match="exceeds the maximum ceiling"):
        build_system_instructions(valid_session, max_prompt_chars=100)


def test_static_spoken_phrases_are_plain_text() -> None:
    for phrase in (
        DETERMINISTIC_GREETING,
        INACTIVITY_NUDGE_TEXT,
        INACTIVITY_GOODBYE_TEXT,
        PROVIDER_ERROR_GOODBYE_TEXT,
    ):
        assert phrase.strip()
        assert "*" not in phrase
        assert "#" not in phrase
        assert "`" not in phrase
