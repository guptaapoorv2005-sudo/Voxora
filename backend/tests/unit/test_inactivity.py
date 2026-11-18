"""Deterministic unit tests for CallInactivityController timing, races, and shutdown."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.voice.inactivity import CallInactivityController
from app.voice.prompts import INACTIVITY_GOODBYE_TEXT, INACTIVITY_NUDGE_TEXT


def _make_fake_session() -> MagicMock:
    session = MagicMock()
    session.say = AsyncMock()
    session.shutdown = MagicMock()
    return session


@pytest.mark.asyncio
async def test_endofturn_to_thinking_race_does_not_arm_inactivity_timer() -> None:
    """Verify Flux EndOfTurn -> agent 'thinking' gap never arms the 10s inactivity timer."""
    session = _make_fake_session()
    controller = CallInactivityController(
        session=session,
        room_name="voxora-001122334455",
        session_id="sess-001122334455",
        nudge_timeout_s=0.06,
        disconnect_timeout_s=0.08,
        turn_handoff_grace_s=0.03,
    )
    try:
        controller.mark_session_ready()
        assert controller.timer_mode == "nudge"

        # Caller speaks (StartOfTurn -> interim/final transcript -> EndOfTurn).
        controller.on_user_state_changed("speaking")
        assert controller.timer_mode == "none"

        controller.on_user_input_transcribed("I want to check availability", is_final=True)
        controller.on_user_state_changed("listening")

        # Critical race check: right after EndOfTurn, agent_state is still "listening",
        # so controller MUST be in settling grace — NOT armed on the inactivity nudge timer.
        assert controller.timer_mode == "handoff_grace"

        # Simulate LiveKit endpointing delay before agent transitions to "thinking".
        await asyncio.sleep(0.01)
        controller.on_agent_state_changed("thinking")
        assert controller.timer_mode == "none"
        assert not controller.has_pending_timer

        # Even waiting longer than nudge_timeout_s while agent is thinking/speaking must
        # never trigger an inactivity nudge.
        await asyncio.sleep(0.08)
        session.say.assert_not_awaited()
        assert not controller.nudge_sent
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_dropped_empty_turn_recovers_after_grace_and_nudges_once() -> None:
    """If EndOfTurn does not produce an agent turn, grace falls through to 10s nudge."""
    session = _make_fake_session()
    controller = CallInactivityController(
        session=session,
        room_name="voxora-001122334455",
        session_id="sess-001122334455",
        nudge_timeout_s=0.06,
        disconnect_timeout_s=0.20,
        turn_handoff_grace_s=0.02,
    )
    try:
        controller.mark_session_ready()
        controller.on_user_state_changed("speaking")
        controller.on_user_state_changed("listening")
        assert controller.timer_mode == "handoff_grace"

        await asyncio.sleep(0.035)
        assert controller.timer_mode == "nudge"

        await asyncio.sleep(0.05)
        session.say.assert_awaited_once_with(INACTIVITY_NUDGE_TEXT, allow_interruptions=True)
        assert controller.nudge_sent is True
        # Now waiting in 20s post-nudge phase; a second nudge never fires.
        assert controller.timer_mode == "disconnect"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_inactivity_timer_resets_when_caller_speaks_again() -> None:
    """Caller StartOfTurn cancels pending nudge timer and resets inactivity cycle."""
    session = _make_fake_session()
    controller = CallInactivityController(
        session=session,
        room_name="voxora-001122334455",
        session_id="sess-001122334455",
        nudge_timeout_s=0.06,
        disconnect_timeout_s=0.08,
        turn_handoff_grace_s=0.02,
    )
    try:
        controller.mark_session_ready()
        await asyncio.sleep(0.03)
        controller.on_user_state_changed("speaking")
        assert controller.timer_mode == "none"

        # Original 0.06s window would have expired by now; verify nudge did not fire.
        await asyncio.sleep(0.05)
        session.say.assert_not_awaited()
        assert not controller.nudge_sent
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_one_nudge_then_20s_post_nudge_wait_before_graceful_termination() -> None:
    """Verify 10s nudge -> wait for playout -> 20s post-nudge wait -> goodbye & shutdown."""
    session = _make_fake_session()
    nudge_started = asyncio.Event()
    allow_nudge_finish = asyncio.Event()
    spoken_texts: list[tuple[str, bool]] = []

    async def fake_say(text: str, *, allow_interruptions: bool = True) -> None:
        spoken_texts.append((text, allow_interruptions))
        if text == INACTIVITY_NUDGE_TEXT:
            nudge_started.set()
            await allow_nudge_finish.wait()

    session.say = AsyncMock(side_effect=fake_say)
    controller = CallInactivityController(
        session=session,
        room_name="voxora-001122334455",
        session_id="sess-001122334455",
        nudge_timeout_s=0.04,
        disconnect_timeout_s=0.05,
        turn_handoff_grace_s=0.015,
    )
    try:
        controller.mark_session_ready()
        await asyncio.wait_for(nudge_started.wait(), timeout=0.5)

        # While the nudge itself is still playing, the post-nudge disconnect timer MUST
        # NOT be armed yet.
        assert controller.nudge_sent is True
        assert controller.timer_mode == "none"

        # Simulate nudge finishing and agent returning to "listening".
        allow_nudge_finish.set()
        await asyncio.sleep(0.01)
        assert controller.timer_mode == "disconnect"
        session.shutdown.assert_not_called()

        # After post-nudge disconnect delay, goodbye is spoken and session shuts down once.
        await asyncio.sleep(0.07)
        assert spoken_texts == [
            (INACTIVITY_NUDGE_TEXT, True),
            (INACTIVITY_GOODBYE_TEXT, False),
        ]
        session.shutdown.assert_called_once_with(drain=True)
        assert controller.closed is True
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_caller_resuming_after_nudge_cancels_disconnect_and_resets_nudge() -> None:
    """Speaking during the 20s post-nudge window cancels disconnect and resets state."""
    session = _make_fake_session()
    controller = CallInactivityController(
        session=session,
        room_name="voxora-001122334455",
        session_id="sess-001122334455",
        nudge_timeout_s=0.03,
        disconnect_timeout_s=0.06,
        turn_handoff_grace_s=0.01,
    )
    try:
        controller.mark_session_ready()
        await asyncio.sleep(0.05)
        assert controller.nudge_sent is True
        assert controller.timer_mode == "disconnect"

        # Caller resumes before 20s expires.
        controller.on_user_state_changed("speaking")
        assert controller.timer_mode == "none"
        assert controller.nudge_sent is False

        await asyncio.sleep(0.08)
        session.shutdown.assert_not_called()
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_aclose_cancels_pending_timers_on_external_shutdown() -> None:
    """Closing the controller cancels armed timers and blocks further nudges."""
    session = _make_fake_session()
    controller = CallInactivityController(
        session=session,
        room_name="voxora-001122334455",
        session_id="sess-001122334455",
        nudge_timeout_s=0.04,
        disconnect_timeout_s=0.04,
        turn_handoff_grace_s=0.01,
    )
    controller.mark_session_ready()
    assert controller.has_pending_timer is True

    await controller.aclose()
    await asyncio.sleep(0.06)

    assert controller.closed is True
    assert controller.has_pending_timer is False
    session.say.assert_not_awaited()
    session.shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_and_concurrent_shutdown_calls_are_idempotent() -> None:
    """Concurrent or repeated shutdown triggers invoke session termination only once."""
    session = _make_fake_session()
    shutdown_calls: list[bool] = []

    async def tracked_shutdown(drain: bool) -> Any:
        await asyncio.sleep(0.01)
        shutdown_calls.append(drain)

    controller = CallInactivityController(
        session=session,
        room_name="voxora-001122334455",
        session_id="sess-001122334455",
        nudge_timeout_s=0.05,
        disconnect_timeout_s=0.05,
        turn_handoff_grace_s=0.01,
        shutdown_fn=tracked_shutdown,
    )
    controller.mark_session_ready()

    await asyncio.gather(
        controller.trigger_shutdown(drain=True),
        controller.trigger_shutdown(drain=False),
        controller.trigger_shutdown(drain=True),
    )
    await controller.trigger_shutdown(drain=True)

    assert shutdown_calls == [True]
    assert controller.closed is True
    assert not controller.has_pending_timer
