"""Unit tests verifying fail-closed session turn budget enforcement (100 turns)."""

from unittest.mock import MagicMock

import pytest

from app.voice.clinic_tools import ClinicVoiceTools, TurnBudgetExceededError


def test_turn_budget_records_monotonic_turns_up_to_100() -> None:
    """Monotonic turn IDs 1 through 100 are recorded cleanly without error."""
    tools = ClinicVoiceTools(
        service=MagicMock(),
        session_factory=MagicMock(),
        clinic_ctx=MagicMock(),
        voice_session_id="sess-test-budget",
        client_id="client-test",
    )

    for i in range(1, 101):
        turn_id = tools.record_turn(f"item-{i}")
        assert turn_id == i


def test_turn_budget_fails_closed_on_turn_101() -> None:
    """Exceeding 100 turns raises TurnBudgetExceededError fail-closed."""
    tools = ClinicVoiceTools(
        service=MagicMock(),
        session_factory=MagicMock(),
        clinic_ctx=MagicMock(),
        voice_session_id="sess-test-budget",
        client_id="client-test",
    )

    for i in range(1, 101):
        tools.record_turn(f"item-{i}")

    with pytest.raises(TurnBudgetExceededError, match="maximum turn budget of 100 turns"):
        tools.record_turn("item-101")


def test_turn_budget_idempotent_for_existing_item() -> None:
    """Re-recording an existing item returns the established turn ID without incrementing."""
    tools = ClinicVoiceTools(
        service=MagicMock(),
        session_factory=MagicMock(),
        clinic_ctx=MagicMock(),
        voice_session_id="sess-test-budget",
        client_id="client-test",
    )

    t1 = tools.record_turn("item-1")
    assert t1 == 1

    t1_again = tools.record_turn("item-1")
    assert t1_again == 1

    t2 = tools.record_turn("item-2")
    assert t2 == 2
