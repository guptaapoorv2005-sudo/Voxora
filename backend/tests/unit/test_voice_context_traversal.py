"""Unit tests for causal backward conversation traversal."""

import pytest
from livekit.agents.llm import ChatMessage, FunctionCall

from app.voice.context_traversal import (
    TurnCorrelationError,
    resolve_causal_parent_user_turn,
)


def test_empty_conversation_raises():
    with pytest.raises(TurnCorrelationError, match="empty"):
        resolve_causal_parent_user_turn([], "call-1")


def test_no_preceding_user_message_raises():
    items = [
        ChatMessage(role="assistant", content=["Hello! How can I help you?"]),
        FunctionCall(call_id="call-1", name="list_services", arguments="{}"),
    ]
    with pytest.raises(TurnCorrelationError, match="No causal parent user message"):
        resolve_causal_parent_user_turn(items, "call-1")


def test_resolve_parent_user_turn_success():
    user_msg = ChatMessage(role="user", content=["Yes, please confirm it."])
    user_msg.id = "user-msg-42"
    user_msg.metrics = {"started_speaking_at": 1700000000.5}

    items = [
        ChatMessage(role="assistant", content=["Shall I book this for you?"]),
        user_msg,
        FunctionCall(
            call_id="call-confirm-1", name="confirm_booking", arguments='{"proposal_id": "prop-1"}'
        ),
    ]

    correlated = resolve_causal_parent_user_turn(
        items,
        "call-confirm-1",
        turn_tracker={"user-msg-42": 3},
    )

    assert correlated.item_id == "user-msg-42"
    assert correlated.transcript == "Yes, please confirm it."
    assert correlated.turn_id == 3
    assert correlated.speech_started_at is not None
    assert correlated.speech_started_at.timestamp() == pytest.approx(1700000000.5)


def test_ignores_older_user_messages_selects_immediate_parent():
    user1 = ChatMessage(role="user", content=["I want to see Dr. Smith"])
    user1.id = "user-1"
    asst1 = ChatMessage(role="assistant", content=["Sure, when?"])
    user2 = ChatMessage(role="user", content=["Tomorrow at 3pm"])
    user2.id = "user-2"
    call = FunctionCall(call_id="call-avail", name="check_availability", arguments="{}")

    items = [user1, asst1, user2, call]

    correlated = resolve_causal_parent_user_turn(
        items,
        "call-avail",
        turn_tracker={"user-1": 1, "user-2": 2},
    )

    assert correlated.item_id == "user-2"
    assert correlated.transcript == "Tomorrow at 3pm"
    assert correlated.turn_id == 2
