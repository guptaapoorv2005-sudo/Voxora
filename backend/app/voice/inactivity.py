"""Application-level call inactivity policy for the Voxora voice receptionist.

Why does Voxora need this when Deepgram Flux already handles turn detection?
- **Turn detection** (handled by Deepgram Flux ``STTv2``) answers: *"Did the
  caller finish speaking their sentence so the AI can reply?"*
- **Call inactivity** (handled here) answers: *"Has the caller gone completely
  silent for too long while the receptionist is waiting for them?"*

Think of this like an idle-session middleware timer in Express, except real-time
voice requires careful event-ordering rules:
1. Never start the idle clock while the AI is still thinking or speaking.
2. Never start the idle clock in the brief gap between Deepgram Flux's
   ``EndOfTurn`` event (which flips ``user_state`` to ``"listening"``) and
   LiveKit transitioning ``agent_state`` to ``"thinking"``.
3. Give **one** polite verbal nudge after 10 seconds of caller silence.
4. Wait **20 more seconds** after the nudge finishes playing; if still silent,
   say goodbye and terminate the session cleanly.
5. Reset immediately whenever the caller speaks again, and cancel all timers
   cleanly on shutdown without double-closing under concurrent triggers.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.voice.events import log_voice_event
from app.voice.prompts import INACTIVITY_GOODBYE_TEXT, INACTIVITY_NUDGE_TEXT

logger = logging.getLogger("voxora.voice.inactivity")


class CallInactivityController:
    """Manages caller inactivity nudges and graceful idle termination for one session."""

    def __init__(
        self,
        *,
        session: Any,
        room_name: str | None,
        session_id: str | None,
        nudge_timeout_s: float = 10.0,
        disconnect_timeout_s: float = 20.0,
        turn_handoff_grace_s: float = 1.5,
        shutdown_fn: Callable[[bool], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        logger_instance: logging.Logger = logger,
    ) -> None:
        if nudge_timeout_s <= 0 or disconnect_timeout_s <= 0:
            raise ValueError("Inactivity timeouts must be positive.")
        if turn_handoff_grace_s <= 0 or turn_handoff_grace_s >= nudge_timeout_s:
            raise ValueError("turn_handoff_grace_s must be in (0, nudge_timeout_s).")

        self._session = session
        self._room_name = room_name
        self._session_id = session_id
        self._nudge_timeout_s = nudge_timeout_s
        self._disconnect_timeout_s = disconnect_timeout_s
        self._turn_handoff_grace_s = turn_handoff_grace_s
        self._shutdown_fn = shutdown_fn
        self._clock = clock
        self._logger = logger_instance

        self._user_state: str = "listening"
        self._agent_state: str = "initializing"
        self._nudge_sent: bool = False
        self._speaking_nudge: bool = False
        self._turn_activity_seen: bool = False
        self._closed: bool = False
        self._shutdown_called: bool = False

        self._active_task: asyncio.Task[None] | None = None
        self._timer_mode: str = "none"
        self._action_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()

    @property
    def nudge_sent(self) -> bool:
        """Whether the one-time 10s inactivity nudge has already been sent."""
        return self._nudge_sent

    @property
    def closed(self) -> bool:
        """Whether the controller has been closed or initiated shutdown."""
        return self._closed

    @property
    def timer_mode(self) -> str:
        """Current timer stage: ``none``, ``handoff_grace``, ``nudge``, or ``disconnect``."""
        if not self.has_pending_timer:
            return "none"
        return self._timer_mode

    @property
    def has_pending_timer(self) -> bool:
        """Return True when a handoff grace, nudge, or disconnect timer is armed."""
        return self._active_task is not None and not self._active_task.done()

    def mark_session_ready(self) -> None:
        """Mark the initial session startup/greeting complete if still initializing."""
        if self._closed:
            return
        if self._agent_state == "initializing":
            self._agent_state = "listening"
        self._reconcile_idle_timer()

    def on_user_state_changed(self, new_state: str) -> None:
        """Handle LiveKit ``user_state_changed`` (driven by Deepgram Flux turns)."""
        if self._closed:
            return

        previous_state = self._user_state
        self._user_state = new_state

        if new_state == "speaking":
            # Deepgram Flux StartOfTurn: caller is active again, reset inactivity state.
            self._cancel_active_timer()
            self._nudge_sent = False
            self._turn_activity_seen = True
            return

        if new_state == "listening":
            if previous_state == "speaking" or self._turn_activity_seen:
                # Flux EndOfTurn flips user_state to "listening" *before* LiveKit finishes
                # its endpointing delay and transitions agent_state to "thinking". Wait for
                # the settling grace window before treating this as idle silence.
                if self._agent_state == "listening":
                    self._schedule_turn_handoff_grace()
                else:
                    self._cancel_active_timer()
            else:
                self._reconcile_idle_timer()
            return

        # Any non-listening state (such as "away" if externally emitted) stops local timers.
        self._cancel_active_timer()

    def on_user_input_transcribed(self, transcript: str, *, is_final: bool = False) -> None:
        """Handle interim/final Flux transcripts to reset inactivity on real speech."""
        if self._closed or not transcript.strip():
            return

        self._cancel_active_timer()
        self._nudge_sent = False
        self._turn_activity_seen = True

        # If a late final transcript arrives while both sides already read "listening",
        # keep the handoff grace armed so we do not start the 10s timer before "thinking".
        if is_final and self._user_state == "listening" and self._agent_state == "listening":
            self._schedule_turn_handoff_grace()

    def on_agent_state_changed(self, new_state: str) -> None:
        """Handle LiveKit ``agent_state_changed`` transitions."""
        if self._closed:
            return

        self._agent_state = new_state

        if new_state in {"initializing", "thinking", "speaking"}:
            # Agent is actively processing or speaking: clear pending turn handoff and
            # suspend all inactivity timers until the agent returns to "listening".
            self._cancel_active_timer()
            self._turn_activity_seen = False
            return

        if new_state == "listening":
            if self._speaking_nudge:
                # _on_nudge_timeout's finally block arms the 20s timer once playout finishes.
                return
            if self._user_state == "listening" and not self._turn_activity_seen:
                self._reconcile_idle_timer()

    def _reconcile_idle_timer(self) -> None:
        """Arm the appropriate inactivity timer when both caller and agent are idle."""
        if self._closed:
            return
        if self._user_state != "listening" or self._agent_state != "listening":
            return
        if self._turn_activity_seen or self._speaking_nudge:
            return

        if self._nudge_sent:
            self._schedule_timer(self._disconnect_timeout_s, self._on_disconnect_timeout)
        else:
            self._schedule_timer(self._nudge_timeout_s, self._on_nudge_timeout)

    def _schedule_turn_handoff_grace(self) -> None:
        """Defer arming the 10s timer briefly while waiting for agent ``thinking`` state."""
        if self._closed or self._agent_state != "listening" or self._user_state != "listening":
            return

        started_at = self._clock()
        self._cancel_active_timer()

        async def _handoff_grace_task() -> None:
            try:
                await asyncio.sleep(self._turn_handoff_grace_s)
            except asyncio.CancelledError:
                return

            # If we reach here without cancellation, LiveKit did not move to "thinking"
            # (for example, an empty noise burst produced no reply). Clear the pending
            # flag and arm only the remaining nudge budget so total silence stays 10s.
            if self._closed or self._user_state != "listening" or self._agent_state != "listening":
                return

            self._turn_activity_seen = False
            elapsed = max(0.0, self._clock() - started_at)
            remaining = max(0.0, self._nudge_timeout_s - elapsed)
            self._timer_mode = "nudge"
            self._active_task = asyncio.create_task(
                self._sleep_and_run(remaining, self._on_nudge_timeout),
                name="voxora-inactivity-nudge",
            )

        self._timer_mode = "handoff_grace"
        self._active_task = asyncio.create_task(
            _handoff_grace_task(),
            name="voxora-turn-handoff-grace",
        )

    def _schedule_timer(
        self,
        delay_s: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        self._cancel_active_timer()
        self._timer_mode = "disconnect" if callback == self._on_disconnect_timeout else "nudge"
        self._active_task = asyncio.create_task(
            self._sleep_and_run(delay_s, callback),
            name=f"voxora-{callback.__name__}",
        )

    async def _sleep_and_run(
        self,
        delay_s: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        await callback()

    def _cancel_active_timer(self) -> None:
        task = self._active_task
        self._active_task = None
        self._timer_mode = "none"
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _speak(self, text: str, *, allow_interruptions: bool) -> None:
        """Speak text via ``session.say`` and await playout if a handle is returned."""
        handle = self._session.say(text, allow_interruptions=allow_interruptions)
        if inspect.isawaitable(handle):
            await handle

    async def _on_nudge_timeout(self) -> None:
        """Send one polite nudge after 10 seconds of idle caller silence."""
        async with self._action_lock:
            if (
                self._closed
                or self._nudge_sent
                or self._turn_activity_seen
                or self._user_state != "listening"
                or self._agent_state != "listening"
            ):
                return

            self._nudge_sent = True
            self._speaking_nudge = True
            self._active_task = None

            log_voice_event(
                self._logger,
                "inactivity_nudge_sent",
                room_name=self._room_name,
                session_id=self._session_id,
            )

            try:
                await self._speak(INACTIVITY_NUDGE_TEXT, allow_interruptions=True)
            except Exception as exc:
                self._logger.warning(
                    "Inactivity nudge speech failed (%s); continuing shutdown guard.",
                    type(exc).__name__,
                )
            finally:
                self._speaking_nudge = False

            # Only start the 20s post-nudge countdown if the caller stayed silent
            # and the agent is back in "listening" (not interrupted by caller speech).
            if (
                not self._closed
                and self._nudge_sent
                and not self._turn_activity_seen
                and self._user_state == "listening"
                and self._agent_state == "listening"
                and not self.has_pending_timer
            ):
                self._schedule_timer(self._disconnect_timeout_s, self._on_disconnect_timeout)

    async def _on_disconnect_timeout(self) -> None:
        """Say goodbye and terminate gracefully after 20s of silence post-nudge."""
        async with self._action_lock:
            if (
                self._closed
                or not self._nudge_sent
                or self._turn_activity_seen
                or self._user_state != "listening"
                or self._agent_state != "listening"
            ):
                return

            self._active_task = None
            log_voice_event(
                self._logger,
                "inactivity_disconnect",
                room_name=self._room_name,
                session_id=self._session_id,
                metadata={"reason": "caller_inactivity_timeout"},
            )

            try:
                await self._speak(INACTIVITY_GOODBYE_TEXT, allow_interruptions=False)
            except Exception as exc:
                self._logger.warning(
                    "Inactivity goodbye speech failed (%s); closing session.",
                    type(exc).__name__,
                )

            await self.trigger_shutdown(drain=True)

    async def trigger_shutdown(self, *, drain: bool = True) -> None:
        """Idempotently shut down the LiveKit session and cancel all pending timers."""
        async with self._shutdown_lock:
            self._closed = True
            self._cancel_active_timer()
            if self._shutdown_called:
                return
            self._shutdown_called = True

            if self._shutdown_fn is not None:
                await self._shutdown_fn(drain)
                return

            result = self._session.shutdown(drain=drain)
            if inspect.isawaitable(result):
                await result

    async def aclose(self) -> None:
        """Cancel pending inactivity timers when the session closes externally."""
        self._closed = True
        self._cancel_active_timer()
