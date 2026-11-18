"""Run against the separately guarded TEST_DATABASE_URL after applying migrations."""
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.call import Call, CallTraceEvent
from app.voice.tracing import CallTraceRecorder

pytestmark = pytest.mark.integration


async def test_trace_persistence_order_and_redaction(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    call_id = f"sess-{uuid4().hex[:12]}"
    trace = CallTraceRecorder(factory, "private room", call_id)
    trace.record("session_started", {"reason": "Private Name", "caller_phone": "9876543210"})
    await trace.flush()
    trace.record_tool("confirm_booking", "failed", 12.5)
    await trace.flush(outcome=trace.outcome)
    async with factory() as db:
        call = await db.get(Call, call_id)
        events = (await db.execute(select(CallTraceEvent).where(CallTraceEvent.call_id == call_id).order_by(CallTraceEvent.sequence))).scalars().all()
        assert call is not None and call.outcome == "needs_attention" and call.ended_at is not None
        assert [event.sequence for event in events] == [1, 2]
        assert "Private Name" not in str([event.details for event in events])
        assert events[1].details["duration_ms"] == 12.5
        await db.execute(delete(Call).where(Call.id == call_id))
        await db.commit()
