"""PostgreSQL proof that concurrent refreshes cannot both consume one token."""

import secrets
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import routes_auth
from app.db.models.owner import OwnerLoginAttempt, OwnerSession, OwnerUser
from app.db.session import get_db


@pytest.mark.integration
async def test_concurrent_refresh_consumes_token_once(test_engine, test_db_url, monkeypatch):
    schema = "phase7_" + secrets.token_hex(8)
    async with test_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_db_url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: OwnerUser.__table__.create(c))
            await conn.run_sync(lambda c: OwnerSession.__table__.create(c))
            await conn.run_sync(lambda c: OwnerLoginAttempt.__table__.create(c))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(
                OwnerUser(
                    email="owner@example.test",
                    hashed_password=routes_auth.password_hash.hash(
                        "a test password of adequate length"
                    ),
                    is_active=True,
                )
            )
            await db.commit()
        monkeypatch.setattr(
            routes_auth,
            "get_settings",
            lambda: SimpleNamespace(
                owner_jwt_secret="x" * 48,
                allowed_origins=["https://test"],
                environment="production",
            ),
        )
        app = FastAPI()
        app.include_router(routes_auth.router)

        async def get_test_db():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = get_test_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as client:
            result = await client.post(
                "/api/auth/login",
                headers={"Origin": "https://test"},
                json={
                    "email": "owner@example.test",
                    "password": "a test password of adequate length",
                },
            )
            assert result.status_code == 200
            refresh = client.cookies.get("voxora_refresh")
        import asyncio

        async def rotate():
            async with AsyncClient(transport=transport, base_url="https://test") as client:
                client.cookies.set("voxora_refresh", refresh)
                return await client.post("/api/auth/refresh", headers={"Origin": "https://test"})

        responses = await asyncio.gather(rotate(), rotate())
        assert sorted(r.status_code for r in responses) == [200, 401]
    finally:
        await engine.dispose()
        async with test_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.mark.integration
async def test_owner_edit_is_loaded_by_new_voice_context(test_engine, test_db_url, monkeypatch):
    from app.api import routes_owner
    from app.db.models.clinic import (
        FAQ,
        BusinessClosure,
        BusinessHours,
        BusinessProfile,
        ClinicPolicy,
        Service,
    )
    from app.seed.brightsmile import seed_brightsmile
    from app.services.clinic_context import load_clinic_context

    schema = "phase7_context_" + secrets.token_hex(8)
    async with test_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_db_url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            for model in (
                BusinessProfile,
                BusinessHours,
                BusinessClosure,
                Service,
                ClinicPolicy,
                FAQ,
                OwnerUser,
                OwnerSession,
                OwnerLoginAttempt,
            ):
                await conn.run_sync(lambda c, table=model.__table__: table.create(c))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await seed_brightsmile(db)
            db.add(
                OwnerUser(
                    email="owner@example.test",
                    hashed_password=routes_auth.password_hash.hash(
                        "a test password of adequate length"
                    ),
                    is_active=True,
                )
            )
            await db.commit()
        monkeypatch.setattr(
            routes_auth,
            "get_settings",
            lambda: SimpleNamespace(
                owner_jwt_secret="x" * 48,
                allowed_origins=["https://test"],
                environment="production",
            ),
        )
        app = FastAPI()
        app.include_router(routes_auth.router)
        app.include_router(routes_owner.router)

        async def get_test_db():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = get_test_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            origin = {"Origin": "https://test"}
            assert (await client.get("/api/owner/profile")).status_code == 401
            response = await client.post(
                "/api/auth/login",
                headers=origin,
                json={
                    "email": "owner@example.test",
                    "password": "a test password of adequate length",
                },
            )
            assert response.status_code == 200
            profile = (await client.get("/api/owner/profile")).json()
            profile = {key: profile[key] for key in routes_owner.PROFILE_FIELDS}
            profile["name"] = "Updated Demo Clinic"
            assert (
                await client.put("/api/owner/profile", headers=origin, json=profile)
            ).status_code == 200
            assert (
                await client.put("/api/owner/receptionist", headers=origin, json={"enabled": False})
            ).status_code == 200
            assert not (await client.get("/api/owner/profile")).json()["receptionist_enabled"]
        async with factory() as db:
            assert (await load_clinic_context(db)).profile.name == "Updated Demo Clinic"
    finally:
        await engine.dispose()
        async with test_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
