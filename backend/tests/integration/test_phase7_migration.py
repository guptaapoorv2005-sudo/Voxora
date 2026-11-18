"""Exercise the phase 7 upgrade/downgrade in a disposable PostgreSQL schema."""

import importlib
import secrets

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.integration
async def test_phase7_upgrade_and_downgrade(test_engine, test_db_url, monkeypatch):
    schema = "phase7_migration_" + secrets.token_hex(8)
    async with test_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_db_url, connect_args={"server_settings": {"search_path": schema}}
    )
    migration = importlib.import_module("app.db.migrations.versions.a8b9c0d1e2f3_owner_sessions")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE business_profile (id integer PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE owner_users (id integer PRIMARY KEY)"))

            def upgrade(sync_conn):
                monkeypatch.setattr(
                    migration, "op", Operations(MigrationContext.configure(sync_conn))
                )
                migration.upgrade()
                inspector = inspect(sync_conn)
                assert "owner_sessions" in inspector.get_table_names()
                assert "owner_login_attempts" in inspector.get_table_names()
                assert "receptionist_enabled" in {
                    column["name"] for column in inspector.get_columns("business_profile")
                }

            await conn.run_sync(upgrade)
        async with engine.begin() as conn:

            def downgrade(sync_conn):
                monkeypatch.setattr(
                    migration, "op", Operations(MigrationContext.configure(sync_conn))
                )
                migration.downgrade()
                inspector = inspect(sync_conn)
                assert "owner_sessions" not in inspector.get_table_names()
                assert "receptionist_enabled" not in {
                    column["name"] for column in inspector.get_columns("business_profile")
                }

            await conn.run_sync(downgrade)
    finally:
        await engine.dispose()
        async with test_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
