"""Pytest configuration and fixtures for Phase 4 integration and concurrency tests.

Critical Fail-Closed Test Database Isolation Guard:
Integration tests run exclusively against an isolated PostgreSQL test database target.
This fixture strictly enforces:
1. TEST_DATABASE_URL presence (skips if absent).
2. Database name suffix ending with '_test' (^[a-zA-Z0-9_-]+_test$).
3. Strict dissimilarity from production DATABASE_URL.
4. Active SQL session current_database() validation.
5. Canary table write verification (_voxora_test_isolation_canary).
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import normalize_asyncpg_url

TEST_DB_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]+_test$")


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url_str = os.getenv("TEST_DATABASE_URL")
    if not url_str or not url_str.strip():
        pytest.skip(
            "Integration tests skipped: TEST_DATABASE_URL is not set. "
            "To run integration tests, provision an isolated test database and "
            "set TEST_DATABASE_URL in .env (database name must end with '_test')."
        )

    norm_url_str = normalize_asyncpg_url(url_str.strip())
    test_url = make_url(norm_url_str)
    prod_url_str = os.getenv("DATABASE_URL")

    # 1. Enforce strict database naming convention: MUST match ^[a-zA-Z0-9_-]+_test$
    db_name = test_url.database or ""
    if not TEST_DB_NAME_REGEX.fullmatch(db_name):
        pytest.exit(
            f"CRITICAL: TEST_DATABASE_URL database name '{db_name}' must end with '_test'. Aborting.",
            returncode=1,
        )

    # 2. Refuse identical database identity
    if prod_url_str:
        prod_url = make_url(normalize_asyncpg_url(prod_url_str.strip()))
        test_port = test_url.port or 5432
        prod_port = prod_url.port or 5432
        if (
            test_url.host == prod_url.host
            and test_port == prod_port
            and test_url.database == prod_url.database
        ):
            pytest.exit(
                "CRITICAL: TEST_DATABASE_URL points to the same database as DATABASE_URL. Aborting.",
                returncode=1,
            )

    return norm_url_str


@pytest.fixture(scope="session")
async def test_engine(test_db_url: str) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(test_db_url, echo=False, pool_pre_ping=True)

    # 3. Active in-session SQL database identity check
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT current_database();"))
        actual_db = res.scalar()
        if not actual_db or not TEST_DB_NAME_REGEX.fullmatch(actual_db):
            pytest.exit(
                f"CRITICAL: Connected PostgreSQL session database '{actual_db}' is not a verified test database.",
                returncode=1,
            )

        # 4. Canary table validation
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS _voxora_test_isolation_canary ("
                "canary_id INTEGER PRIMARY KEY, is_test_database BOOLEAN NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW());"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO _voxora_test_isolation_canary (canary_id, is_test_database) VALUES (1, TRUE) "
                "ON CONFLICT (canary_id) DO NOTHING;"
            )
        )
        await conn.commit()

    yield engine
    await engine.dispose()


@pytest.fixture
async def test_db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session
