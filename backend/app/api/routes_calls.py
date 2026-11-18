"""Development-only trace inspection; owner authorization arrives in phase 7."""
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.call import Call, CallTraceEvent
from app.db.session import get_db

router = APIRouter(prefix="/api/calls", tags=["call traces"])


@router.get("/{call_id}")
async def call_detail(call_id: str, db: AsyncSession = Depends(get_db),
                      trace_key: str | None = Header(default=None, alias="X-Trace-Key")) -> dict[str, Any]:
    settings = get_settings()
    secret = settings.trace_access_secret
    if (settings.environment.lower() != "development" or not secret or len(secret) < 32
            or not trace_key or not secrets.compare_digest(secret.encode(), trace_key.encode())):
        raise HTTPException(status_code=404, detail="Not found")
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Not found")
    result = await db.execute(select(CallTraceEvent).where(CallTraceEvent.call_id == call_id).order_by(CallTraceEvent.sequence))
    return {"id": call.id, "started_at": call.started_at, "ended_at": call.ended_at,
            "outcome": call.outcome, "events": [
                {"sequence": e.sequence, "occurred_at": e.occurred_at, "event": e.event, "details": e.details}
                for e in result.scalars().all()]}
