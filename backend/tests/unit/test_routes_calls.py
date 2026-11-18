from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import routes_calls
from app.db.session import get_db


async def test_call_trace_disabled_outside_development(monkeypatch):
    monkeypatch.setattr(routes_calls, "get_settings", lambda: SimpleNamespace(environment="production", trace_access_secret="x" * 32))
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await routes_calls.call_detail("sess-abcdef123456", db, "x" * 32)
    assert exc.value.status_code == 404
    db.get.assert_not_called()


async def test_call_detail_returns_ordered_redacted_events(monkeypatch):
    monkeypatch.setattr(routes_calls, "get_settings", lambda: SimpleNamespace(environment="development", trace_access_secret="x" * 32))
    now = datetime.now(UTC)
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id="sess-abcdef123456", started_at=now, ended_at=now, outcome="needs_attention")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [SimpleNamespace(sequence=1, occurred_at=now, event="tool_completed", details={"tool": "confirm_booking", "result": "failed", "duration_ms": 7.1})]
    db.execute.return_value = result
    data = await routes_calls.call_detail("sess-abcdef123456", db, "x" * 32)
    assert data["outcome"] == "needs_attention"
    assert data["events"][0]["details"]["result"] == "failed"


@pytest.mark.parametrize("configured,supplied", [(None, None), ("short", "short"), ("x" * 32, None), ("x" * 32, "wrong")])
async def test_call_detail_requires_secret(monkeypatch, configured, supplied):
    monkeypatch.setattr(routes_calls, "get_settings", lambda: SimpleNamespace(environment="development", trace_access_secret=configured))
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await routes_calls.call_detail("sess-abcdef123456", db, supplied)
    assert exc.value.status_code == 404
    db.get.assert_not_called()


async def test_http_trace_endpoint_requires_header(monkeypatch):
    secret = "a" * 32
    monkeypatch.setattr(routes_calls, "get_settings", lambda: SimpleNamespace(environment="development", trace_access_secret=secret))
    now = datetime.now(UTC)
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id="sess-abcdef123456", started_at=now, ended_at=now, outcome="completed")
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute.return_value = result
    app = FastAPI()
    app.include_router(routes_calls.router)
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/calls/sess-abcdef123456")).status_code == 404
        assert (await client.get("/api/calls/sess-abcdef123456", headers={"X-Trace-Key": "invalid"})).status_code == 404
        assert (await client.get("/api/calls/sess-abcdef123456", headers={"X-Trace-Key": secret})).status_code == 200
