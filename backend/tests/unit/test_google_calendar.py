from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request, Response

from app.api import routes_google_calendar
from app.api.routes_auth import require_owner
from app.db.session import get_db
from app.main import app
from app.services.google_calendar import event_payload, sync_event


@pytest.mark.asyncio
async def test_calendar_status_route_precedes_owner_collection_route():
    db = AsyncMock()
    db.get.return_value = None
    app.dependency_overrides[require_owner] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test", trust_env=False) as client:
            response = await client.get("/api/owner/google-calendar")
        assert response.status_code == 200
        assert response.json() == {"connected": False, "needs_attention": False}
    finally:
        app.dependency_overrides.pop(require_owner, None)
        app.dependency_overrides.pop(get_db, None)


def appointment(status="confirmed"):
    start = datetime(2026, 12, 1, 10, tzinfo=UTC)
    return SimpleNamespace(status=status, start_at=start, end_at=start + timedelta(minutes=30),
                           service_name_snapshot="Consultation", caller_name="Alex")


@pytest.mark.asyncio
async def test_booking_and_reschedule_use_identifiable_event_without_contact_details():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        await sync_event(client, "token", appointment(), "abc123", False)
        await sync_event(client, "token", appointment(), "abc123", True)
    assert [r.method for r in requests] == ["POST", "PATCH"]
    assert requests[1].url.path.endswith("/abc123")
    assert event_payload(appointment(), "abc123")["summary"] == "Consultation — Alex"
    assert b"phone" not in requests[0].content


@pytest.mark.asyncio
async def test_cancel_is_idempotent_when_event_already_deleted():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(410)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        await sync_event(client, "token", appointment("cancelled"), "abc123", True)
        await sync_event(client, "token", appointment("cancelled"), "abc123", False)
    assert [request.method for request in requests] == ["DELETE", "DELETE"]


@pytest.mark.asyncio
async def test_retry_after_create_succeeded_but_database_commit_failed():
    methods = []

    def handle(request):
        methods.append(request.method)
        return httpx.Response(409 if request.method == "POST" else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        await sync_event(client, "token", appointment(), "abc123", False)
    assert methods == ["POST", "PATCH"]


@pytest.mark.asyncio
async def test_failed_google_request_is_not_marked_successful():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)), trust_env=False) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await sync_event(client, "token", appointment(), "abc123", False)


@pytest.mark.asyncio
async def test_oauth_state_is_bound_to_browser_and_owner_session(monkeypatch):
    key = "a" * 32
    cfg = SimpleNamespace(google_calendar_client_id="client", google_calendar_client_secret="secret",
                          google_calendar_redirect_uri="http://localhost/callback",
                          google_calendar_encryption_key=Fernet.generate_key().decode(),
                          environment="development")
    monkeypatch.setattr(routes_google_calendar, "get_settings", lambda: cfg)
    monkeypatch.setattr(routes_google_calendar, "signing_key", lambda: key)
    access = jwt.encode({"sid": "session-1"}, key, algorithm="HS256")
    request = Request({"type": "http", "headers": [(b"cookie", f"voxora_access={access}".encode())]})
    response = Response()
    result = await routes_google_calendar.connect(request, response, SimpleNamespace(id=42))
    assert "accounts.google.com" in result["url"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    bad_callback = Request({"type": "http", "query_string": b"state=wrong", "headers": []})
    with pytest.raises(HTTPException) as exc:
        await routes_google_calendar.callback(bad_callback, None)
    assert exc.value.status_code == 400
