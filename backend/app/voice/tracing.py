"""Persist redacted operational events in the order observed by one worker session.

No transcripts, tool arguments, exception messages or audio are accepted here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.call import Call, CallTraceEvent
from app.voice.events import VoiceEventType, build_voice_event_payload

logger = logging.getLogger(__name__)


class CallTraceRecorder:
    def __init__(self, factory: async_sessionmaker[AsyncSession], room: str | None, session_id: str):
        self.factory = factory
        self.room = room
        self.session_id = session_id
        self.outcome = "completed"
        self._sequence = 0
        self._pending: list[CallTraceEvent] = []
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._started_at = datetime.now(UTC)

    def record(self, event: VoiceEventType, metadata: dict[str, Any] | None = None) -> None:
        payload = build_voice_event_payload(event, room_name=self.room, session_id=self.session_id, metadata=metadata)
        if event == "provider_error":
            self.outcome = "provider_error"
        metadata = payload.get("meta", {})
        safe_details = {key: value for key, value in metadata.items()
                        if isinstance(value, (bool, int, float))}
        if metadata.get("stage") in {"stt", "llm", "tts", "realtime_llm", "unknown", "assistant_turn"}:
            safe_details["stage"] = metadata["stage"]
        for key in ("old_state", "new_state"):
            if metadata.get(key) in {"initializing", "listening", "speaking", "thinking", "idle"}:
                safe_details[key] = metadata[key]
        self._sequence += 1
        self._pending.append(CallTraceEvent(call_id=payload["session_id"], sequence=self._sequence,
            occurred_at=datetime.fromisoformat(payload["ts"]), event=payload["event"], details=safe_details))

    def record_tool(self, name: str, result: str, duration_ms: float, reason: str | None = None) -> None:
        allowed = {"get_clinic_info", "list_services", "check_availability", "create_booking_proposal", "confirm_booking", "lookup_appointment", "verify_appointment_step_up", "create_reschedule_proposal", "confirm_reschedule", "create_cancellation_proposal", "confirm_cancellation", "request_human_handoff", "escalate_medical_emergency"}
        if name not in allowed or result not in {"error", "failed", "confirmed", "cancelled", "completed", "readback"}:
            raise ValueError("Unknown tool event")
        if reason not in {None, "unexpected_error", "slot_conflict", "missing_caller_turn", "verification_required", "confirmation_rejected", "operation_failed", "proposal_expired", "stale_appointment"}:
            raise ValueError("Unknown tool failure category")
        if result in {"confirmed", "cancelled"}:
            self.outcome = result
        elif result in {"failed", "error"} and self.outcome == "completed":
            self.outcome = "needs_attention"
        self._sequence += 1
        safe_id = build_voice_event_payload("session_started", room_name=self.room, session_id=self.session_id)["session_id"]
        details: dict[str, Any] = {"tool": name, "result": result, "duration_ms": max(0, round(duration_ms, 2))}
        if reason:
            details["reason"] = reason
        self._pending.append(CallTraceEvent(call_id=safe_id, sequence=self._sequence,
            occurred_at=datetime.now(UTC), event="tool_completed",
            details=details))

    def record_tool_started(self, name: str) -> None:
        allowed = {"get_clinic_info", "list_services", "check_availability", "create_booking_proposal", "confirm_booking", "lookup_appointment", "verify_appointment_step_up", "create_reschedule_proposal", "confirm_reschedule", "create_cancellation_proposal", "confirm_cancellation", "request_human_handoff", "escalate_medical_emergency"}
        if name not in allowed:
            raise ValueError("Unknown tool")
        self._sequence += 1
        safe_id = build_voice_event_payload("session_started", room_name=self.room, session_id=self.session_id)["session_id"]
        self._pending.append(CallTraceEvent(call_id=safe_id, sequence=self._sequence,
            occurred_at=datetime.now(UTC), event="tool_started", details={"tool": name}))

    def start_stage(self, stage: str) -> tuple[int, float]:
        if stage not in {"llm", "tts"}:
            raise ValueError("Unknown stage")
        self._sequence += 1
        sequence = self._sequence
        safe_id = build_voice_event_payload("session_started", room_name=self.room, session_id=self.session_id)["session_id"]
        self._pending.append(CallTraceEvent(call_id=safe_id, sequence=sequence,
            occurred_at=datetime.now(UTC), event="stage_started", details={"stage": stage}))
        return sequence, time.perf_counter()

    def end_stage(self, stage: str, started: tuple[int, float], status: str) -> None:
        if stage not in {"llm", "tts"} or status not in {"completed", "interrupted", "failed"}:
            raise ValueError("Unknown stage outcome")
        self._sequence += 1
        safe_id = build_voice_event_payload("session_started", room_name=self.room, session_id=self.session_id)["session_id"]
        self._pending.append(CallTraceEvent(call_id=safe_id, sequence=self._sequence,
            occurred_at=datetime.now(UTC), event="stage_finished", details={"stage": stage,
                "start_sequence": started[0], "duration_ms": round((time.perf_counter() - started[1]) * 1000, 2), "result": status}))

    async def run(self) -> None:
        """Persist during the call and retry transient database errors."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2)
            except TimeoutError:
                await self.flush()

    def stop(self) -> None:
        self._stop.set()

    async def flush(self, *, outcome: str | None = None) -> None:
        async with self._lock:
            batch, self._pending = self._pending, []
            safe_id = build_voice_event_payload("session_started", room_name=self.room, session_id=self.session_id)
            try:
                async with self.factory() as db:
                    call = await db.get(Call, safe_id["session_id"])
                    if call is None:
                        call = Call(id=safe_id["session_id"], room=safe_id["room"], started_at=self._started_at, outcome="in_progress")
                        db.add(call)
                        await db.flush()
                    if outcome is not None:
                        call.outcome = outcome
                        call.ended_at = datetime.now(UTC)
                    if batch:
                        rows = [{"call_id": e.call_id, "sequence": e.sequence,
                                 "occurred_at": e.occurred_at, "event": e.event,
                                 "details": e.details} for e in batch]
                        await db.execute(insert(CallTraceEvent).values(rows).on_conflict_do_nothing(
                            index_elements=["call_id", "sequence"]))
                    await db.commit()
            except Exception:
                self._pending = batch + self._pending
                logger.warning("Call trace persistence failed; events remain queued", exc_info=False)
