from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import routes_auth, routes_owner, routes_session
from app.db.models.owner import OwnerSession, OwnerUser
from app.db.session import get_db


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDB:
    def __init__(self):
        self.owner = OwnerUser(
            id=1,
            email="owner@example.test",
            is_active=True,
            hashed_password=routes_auth.password_hash.hash("correct horse battery staple"),
        )
        self.session = None
        self.attempts = 0

    async def execute(self, statement):
        sql = str(statement)
        if "FROM owner_users" in sql:
            return Result(self.owner)
        if "FROM owner_sessions" in sql:
            return Result(self.session)
        return Result(None)

    async def scalar(self, statement):
        return self.attempts

    async def get(self, model, key):
        if model is OwnerUser:
            return self.owner if key == self.owner.id else None
        return self.session if self.session and self.session.id == key else None

    def add(self, obj):
        if isinstance(obj, OwnerSession):
            self.session = obj
        else:
            self.attempts += 1

    async def commit(self):
        pass


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setattr(
        routes_auth,
        "get_settings",
        lambda: SimpleNamespace(
            owner_jwt_secret="s" * 48, allowed_origins=["http://test"], environment="production"
        ),
    )
    db = FakeDB()
    app = FastAPI()
    app.include_router(routes_auth.router)
    app.include_router(routes_owner.router)
    app.dependency_overrides[get_db] = lambda: db
    return app, db


async def test_login_rotation_replay_logout_and_owner_access(app_and_db):
    app, db = app_and_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        assert (await client.get("/api/auth/me")).status_code == 401
        response = await client.post(
            "/api/auth/login",
            headers={"Origin": "http://test"},
            json={"email": "owner@example.test", "password": "correct horse battery staple"},
        )
        assert response.status_code == 200
        assert all(
            "HttpOnly" in x and "Secure" in x and "SameSite=strict" in x
            for x in response.headers.get_list("set-cookie")
        )
        old_refresh = client.cookies.get("voxora_refresh")
        old_access = client.cookies.get("voxora_access")
        assert old_refresh and old_refresh.split(".")[1] not in db.session.refresh_token_hash
        assert (await client.get("/api/auth/me")).status_code == 200
        assert (
            await client.post("/api/auth/refresh", headers={"Origin": "http://test"})
        ).status_code == 200
        assert client.cookies.get("voxora_refresh") != old_refresh
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as replay:
            replay.cookies.set("voxora_refresh", old_refresh)
            assert (
                await replay.post("/api/auth/refresh", headers={"Origin": "http://test"})
            ).status_code == 401
        assert (
            await client.post("/api/auth/logout", headers={"Origin": "http://test"})
        ).status_code == 200
        assert db.session.revoked_at is not None
        assert (await client.get("/api/auth/me")).status_code == 401
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as replay:
            replay.cookies.set("voxora_access", old_access)
            assert (await replay.get("/api/auth/me")).status_code == 401


async def test_auth_rejects_origins_invalid_credentials_and_expired_session(app_and_db):
    app, db = app_and_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        payload = {"email": "owner@example.test", "password": "wrong"}
        assert (await client.post("/api/auth/login", json=payload)).status_code == 403
        assert (
            await client.post(
                "/api/auth/login", headers={"Origin": "http://evil.test"}, json=payload
            )
        ).status_code == 403
        assert (
            await client.post("/api/auth/login", headers={"Origin": "http://test"}, json=payload)
        ).status_code == 401
        db.attempts = 10
        assert (
            await client.post("/api/auth/login", headers={"Origin": "http://test"}, json=payload)
        ).status_code == 429
        assert (await client.get("/api/owner/profile")).status_code == 401
        db.attempts = 0
        payload["password"] = "correct horse battery staple"
        assert (
            await client.post("/api/auth/login", headers={"Origin": "http://test"}, json=payload)
        ).status_code == 200
        db.session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert (await client.get("/api/auth/me")).status_code == 401
        assert (
            await client.post("/api/auth/refresh", headers={"Origin": "http://test"})
        ).status_code == 401


async def test_origin_checked_on_owner_mutations(app_and_db):
    app, db = app_and_db
    app.dependency_overrides[routes_auth.require_owner] = lambda: db.owner
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.put(
            "/api/owner/receptionist",
            json={"enabled": False},
            headers={"Origin": "https://evil.test"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize("value", [False, None])
async def test_disabled_receptionist_cannot_issue_token(value):
    settings = SimpleNamespace(environment="development")
    db = AsyncMock()
    db.scalar.return_value = value
    with pytest.raises(routes_session.HTTPException) as error:
        await routes_session.create_session_token(settings=settings, db=db)
    assert error.value.status_code == 503


def test_hours_reject_invalid_intervals():
    with pytest.raises(ValueError):
        routes_owner.Hours(
            day_of_week=0,
            is_closed=False,
            open_time="09:00",
            close_time="17:00",
            break_start="18:00",
            break_end="19:00",
        )
    with pytest.raises(ValueError):
        routes_owner.Hours(day_of_week=0, is_closed=True, open_time="09:00")


async def test_profile_update_commits_validated_values(monkeypatch):
    profile = SimpleNamespace()
    db = AsyncMock()
    monkeypatch.setattr(routes_owner, "clinic", AsyncMock(return_value=profile))
    commit = AsyncMock()
    monkeypatch.setattr(routes_owner, "commit_context", commit)
    data = routes_owner.Profile(
        name="New name",
        description="Demo",
        address_line1="Street",
        city="Delhi",
        state="Delhi",
        postal_code="110001",
        country="India",
    )
    assert await routes_owner.update_profile(data, db) == {"ok": True}
    assert profile.name == "New name"
    commit.assert_awaited_once_with(db)


async def test_clinic_edits_commit_without_building_a_prompt():
    db = AsyncMock()
    await routes_owner.commit_context(db)
    db.commit.assert_awaited_once()


async def test_worker_receptionist_switch_fails_closed():
    from worker import receptionist_enabled

    db = AsyncMock()
    for value in (False, None):
        db.scalar.return_value = value
        assert not await receptionist_enabled(db)
    db.scalar.return_value = True
    assert await receptionist_enabled(db)
