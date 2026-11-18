from unittest.mock import MagicMock

import pytest

from app.voice.tracing import CallTraceRecorder


def test_trace_order_and_redaction():
    trace = CallTraceRecorder(MagicMock(), "Alice", "sess-abcdef123456")
    trace.record("user_transcript_received", {"reason": "Alice", "caller_phone": "1234567890", "is_final": True})
    trace.record("turn_metrics_recorded", {"stage": "stt", "transcription_delay_ms": 123})
    assert [event.sequence for event in trace._pending] == [1, 2]
    assert trace._pending[0].details == {"is_final": True}
    assert trace._pending[1].details == {"stage": "stt", "transcription_delay_ms": 123}


def test_only_known_state_labels_are_recorded():
    trace = CallTraceRecorder(MagicMock(), "room", "sess-abcdef123456")
    trace.record("agent_state_changed", {"old_state": "private name", "new_state": "speaking"})
    assert trace._pending[0].details == {"new_state": "speaking"}


@pytest.mark.asyncio
async def test_failed_flush_retains_events_for_retry():
    trace = CallTraceRecorder(MagicMock(side_effect=RuntimeError("private error")), "room", "sess-abcdef123456")
    trace.record("provider_error", {"stage": "llm"})
    await trace.flush(outcome=trace.outcome)
    assert len(trace._pending) == 1
    assert trace.outcome == "provider_error"
