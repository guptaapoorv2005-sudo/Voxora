import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.logging import JSONFormatter
from app.main import app


@pytest.mark.asyncio
async def test_legacy_mutations_reject_untrusted_requests():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in (
            "/api/proposals",
            "/api/proposals/00000000-0000-0000-0000-000000000001/confirm",
        ):
            response = await client.post(path, json={})
            assert response.status_code == 403


def test_log_formatter_removes_contact_details_and_exception_payload():
    try:
        raise ValueError("secret patient@example.com +919876543210")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "contact patient@example.com +919876543210",
            (),
            sys.exc_info(),
        )
    result = json.loads(JSONFormatter().format(record))
    assert "patient@example.com" not in str(result)
    assert "9876543210" not in str(result)
    assert "secret" not in str(result)
    assert result["exception_type"] == "ValueError"


@pytest.mark.asyncio
async def test_development_token_rejects_remote_peer(monkeypatch):
    from unittest.mock import AsyncMock

    from app.core.config import AppSettings, get_settings
    from app.db.session import get_db

    settings = AppSettings(
        _env_file=None,
        environment="development",
        database_url="postgresql+asyncpg://test:test@localhost/test",
        livekit_url="wss://example.invalid",
        livekit_api_key="test",
        livekit_api_secret="test",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        transport = ASGITransport(app=app, client=("203.0.113.10", 1234))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/sessions/token")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_db, None)
