"""Causal backward conversation traversal for tool-to-user-turn correlation.

Locates the tool call item by its call_id in the conversation items list, then
walks backward to locate the immediate parent user turn (ChatMessage with role='user').
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from livekit.agents.llm import ChatMessage, FunctionCall


@dataclass(frozen=True)
class CorrelatedCallerTurn:
    """Immutable representation of the correlated caller turn that triggered a tool call."""

    item_id: str
    transcript: str
    speech_started_at: datetime | None
    turn_id: int


class TurnCorrelationError(Exception):
    """Raised when a tool invocation cannot be causally correlated to a valid caller turn."""


def resolve_causal_parent_user_turn(
    conversation_items: list[Any],
    call_id: str,
    turn_tracker: dict[str, int] | None = None,
) -> CorrelatedCallerTurn:
    """Traverse backward from function call item to find its parent caller message.

    Args:
        conversation_items: List of items from ctx.session.conversation.items
        call_id: The tool call identifier (ctx.function_call.call_id)
        turn_tracker: Optional map from item_id to monotonic turn_id

    Returns:
        CorrelatedCallerTurn: Immutable record of the causal parent caller message.

    Raises:
        TurnCorrelationError: If the call_id is not found or no preceding user turn exists.
    """
    if not conversation_items:
        raise TurnCorrelationError("Conversation item list is empty; cannot correlate tool call.")

    # 1. Locate the tool call item index
    call_idx: int | None = None
    for idx, item in enumerate(conversation_items):
        # Item may be a FunctionCall or ChatMessage with tool_calls
        if isinstance(item, FunctionCall) and getattr(item, "call_id", None) == call_id:
            call_idx = idx
            break
        elif hasattr(item, "tool_calls") and item.tool_calls:
            for tc in item.tool_calls:
                tc_id = getattr(tc, "call_id", None) or getattr(tc, "id", None)
                if tc_id == call_id:
                    call_idx = idx
                    break
            if call_idx is not None:
                break
        elif getattr(item, "id", None) == call_id or getattr(item, "call_id", None) == call_id:
            call_idx = idx
            break

    # If not found directly, assume it follows the items list
    if call_idx is None:
        call_idx = len(conversation_items)

    # 2. Walk backward from call_idx to find the immediate parent ChatMessage(role="user")
    parent_user_msg: ChatMessage | None = None
    for i in range(call_idx - 1, -1, -1):
        candidate = conversation_items[i]
        if isinstance(candidate, ChatMessage) and candidate.role == "user":
            parent_user_msg = candidate
            break

    if parent_user_msg is None:
        raise TurnCorrelationError(
            f"No causal parent user message found preceding call_id '{call_id}'."
        )

    # 3. Extract metrics and timestamps
    item_id = str(getattr(parent_user_msg, "id", "")) or f"item-{call_id}"
    raw_content = getattr(parent_user_msg, "content", "")
    if isinstance(raw_content, list):
        transcript = " ".join(str(c) for c in raw_content)
    else:
        transcript = str(raw_content or "")

    metrics = getattr(parent_user_msg, "metrics", None) or {}
    started_speaking_at_raw = (
        metrics.get("started_speaking_at") if isinstance(metrics, dict) else None
    )

    speech_started_at: datetime | None = None
    if started_speaking_at_raw is not None:
        try:
            speech_started_at = datetime.fromtimestamp(float(started_speaking_at_raw), tz=UTC)
        except (ValueError, TypeError, OSError):
            speech_started_at = None

    # Resolve monotonic turn_id
    turn_id = (turn_tracker or {}).get(item_id, 1)

    return CorrelatedCallerTurn(
        item_id=item_id,
        transcript=transcript,
        speech_started_at=speech_started_at,
        turn_id=turn_id,
    )
