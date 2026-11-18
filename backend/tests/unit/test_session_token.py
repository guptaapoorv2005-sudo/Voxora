import re
from unittest.mock import AsyncMock

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import AppSettings, get_settings
from app.core.worker_config import WorkerSettings
from app.db.session import get_db
from app.main import app


def make_test_settings(
    monkeypatch: pytest.MonkeyPatch,
    environment: str = "development",
    **overrides: object,
) -> AppSettings:
    """Helper constructing an isolated AppSettings instance."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME_OVERRIDE", raising=False)
    return AppSettings(
        _env_file=None,
        environment=environment,
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def receptionist_enabled():
    db = AsyncMock()
    db.scalar.return_value = True
    app.dependency_overrides[get_db] = lambda: db
    yield db
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://127.0.0.1:8000")


@pytest.mark.asyncio
async def test_token_success_in_development(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="development")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert data["livekit_url"] == "wss://test.livekit.cloud"
        assert re.match(r"^voxora-[a-f0-9]{12}$", data["room_name"])
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_token_dispatch_name_matches_settings(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="development")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        token = response.json()["token"]
        claims = jwt.decode(token, settings.livekit_api_secret, algorithms=["HS256"])
        agent_dispatch = claims["roomConfig"]["agents"][0]["agentName"]
        assert agent_dispatch == settings.effective_agent_name == "voxora-agent"
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_token_dispatch_name_uses_override(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_NAME", raising=False)
    monkeypatch.setenv("LIVEKIT_AGENT_NAME_OVERRIDE", "custom-override")
    # Construct settings AFTER setting the environment override
    settings = AppSettings(
        _env_file=None,
        environment="development",
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    assert settings.effective_agent_name == "custom-override"
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        token = response.json()["token"]
        claims = jwt.decode(token, settings.livekit_api_secret, algorithms=["HS256"])
        assert claims["roomConfig"]["agents"][0]["agentName"] == "custom-override"
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_token_dispatch_name_matches_worker_effective_name(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Test default synchronization
    settings = make_test_settings(monkeypatch, environment="development")
    ws = WorkerSettings(
        _env_file=None,
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
        deepgram_api_key="dg-key",
        google_api_key="g-key",
        elevenlabs_api_key="el-key",
        elevenlabs_voice_id="el-vid",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        token = response.json()["token"]
        claims = jwt.decode(token, settings.livekit_api_secret, algorithms=["HS256"])
        agent_name = claims["roomConfig"]["agents"][0]["agentName"]
        assert agent_name == ws.effective_agent_name == "voxora-agent"
    finally:
        app.dependency_overrides.pop(get_settings, None)

    # 2. Test override synchronization
    monkeypatch.setenv("LIVEKIT_AGENT_NAME_OVERRIDE", "synchronized-override")
    settings_override = AppSettings(
        _env_file=None,
        environment="development",
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    ws_override = WorkerSettings(
        _env_file=None,
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
        deepgram_api_key="dg-key",
        google_api_key="g-key",
        elevenlabs_api_key="el-key",
        elevenlabs_voice_id="el-vid",
    )
    app.dependency_overrides[get_settings] = lambda: settings_override
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        token = response.json()["token"]
        claims = jwt.decode(token, settings_override.livekit_api_secret, algorithms=["HS256"])
        agent_dispatch = claims["roomConfig"]["agents"][0]["agentName"]
        assert agent_dispatch == ws_override.effective_agent_name == "synchronized-override"
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_production_guard_fails_closed_when_missing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = AppSettings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="test-api-key",
        livekit_api_secret="test-api-secret",
    )
    assert settings.environment == "production"
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 403
        assert "only available in development environment" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_production_guard_rejects_production(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="production")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_production_guard_rejects_staging(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="staging")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_production_guard_rejects_test(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="test")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_production_guard_rejects_custom_dev(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="dev")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_production_guard_rejects_empty_string(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_room_name_format(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_test_settings(monkeypatch, environment="development")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        room_name = response.json()["room_name"]
        assert re.match(r"^voxora-[a-f0-9]{12}$", room_name)
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_ignores_client_body(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_test_settings(monkeypatch, environment="development")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post(
            "/api/sessions/token",
            json={"room_name": "malicious-room", "identity": "admin-user"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["room_name"] != "malicious-room"
        assert re.match(r"^voxora-[a-f0-9]{12}$", data["room_name"])
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_unique_caller_identity(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_test_settings(monkeypatch, environment="development")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        res1 = await client.post("/api/sessions/token")
        res2 = await client.post("/api/sessions/token")
        assert res1.status_code == 200 and res2.status_code == 200
        token1 = res1.json()["token"]
        token2 = res2.json()["token"]
        claims1 = jwt.decode(token1, settings.livekit_api_secret, algorithms=["HS256"])
        claims2 = jwt.decode(token2, settings.livekit_api_secret, algorithms=["HS256"])
        assert claims1["sub"] != claims2["sub"]
        assert re.match(r"^caller-[a-f0-9]{8}$", claims1["sub"])
        assert re.match(r"^caller-[a-f0-9]{8}$", claims2["sub"])
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_token_ttl_duration(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_test_settings(monkeypatch, environment="development")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        token = response.json()["token"]
        claims = jwt.decode(token, settings.livekit_api_secret, algorithms=["HS256"])
        # TTL duration is exp - iat or exp - nbf (300 seconds)
        duration = claims["exp"] - claims.get("nbf", claims.get("iat"))
        assert duration == 300
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_token_microphone_only_grant(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="development")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        token = response.json()["token"]
        claims = jwt.decode(token, settings.livekit_api_secret, algorithms=["HS256"])
        video = claims["video"]
        assert video["roomJoin"] is True
        assert video["canPublish"] is True
        assert video["canPublishSources"] == ["microphone"]
        assert video["canPublishData"] is False
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_response_has_no_cache_header(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(monkeypatch, environment="development")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = await client.post("/api/sessions/token")
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"
    finally:
        app.dependency_overrides.pop(get_settings, None)
