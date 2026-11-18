"""Integration tests verifying unified L1->L5 lock hierarchy and deadlock-free concurrency."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.booking import Appointment, CallerVerificationSession

pytestmark = pytest.mark.asyncio


async def test_concurrent_lock_hierarchy_adheres_to_l1_to_l5(test_db_url: str) -> None:
    """Concurrent workers acquiring locks in strict order (L1 Session -> L2 Appointment -> L5 Advisory) do not deadlock."""
    engine = create_async_engine(test_db_url, pool_size=10, max_overflow=5)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    now_utc = datetime.now(UTC)
    session_ids = [f"sess-lock-{uuid4().hex[:8]}" for _ in range(3)]

    # Pre-seed appointments and sessions
    async with session_factory() as session:
        for sid in session_ids:
            apt = Appointment(
                clinic_id=1,
                service_id=1,
                service_name_snapshot="General Dental Checkup",
                caller_name="Priya Sharma",
                caller_phone="+919876543210",
                start_at=now_utc + timedelta(days=5),
                end_at=now_utc + timedelta(days=5, minutes=30),
                duration_minutes=30,
                buffer_minutes=15,
                blocked_until_at=now_utc + timedelta(days=5, minutes=45),
                status="confirmed",
                version=1,
            )
            session.add(apt)
            await session.flush()

            v_sess = CallerVerificationSession(
                session_id=sid,
                clinic_id=1,
                verified_appointment_id=apt.id,
                verified_at=now_utc,
            )
            session.add(v_sess)
        await session.commit()

    async def _worker_task(sid: str) -> None:
        async with session_factory() as s:
            # L1: Session lock
            s_row = (
                await s.execute(
                    text(
                        "SELECT * FROM caller_verification_sessions WHERE session_id = :sid FOR UPDATE;"
                    ),
                    {"sid": sid},
                )
            ).fetchone()
            assert s_row is not None
            apt_id = s_row.verified_appointment_id

            # L2: Appointment lock
            apt_row = (
                await s.execute(
                    text("SELECT * FROM appointments WHERE id = :aid FOR UPDATE;"),
                    {"aid": apt_id},
                )
            ).fetchone()
            assert apt_row is not None

            # L5: Advisory lock (date hashtext)
            date_key = 20261120
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('calendar_schedule'), :dk);"),
                {"dk": date_key},
            )
            await asyncio.sleep(0.05)
            await s.commit()

    # Execute all concurrently: strict order guarantees 0 deadlocks
    results = await asyncio.gather(
        *[_worker_task(sid) for sid in session_ids], return_exceptions=True
    )
    for res in results:
        assert not isinstance(res, Exception), f"Worker failed with: {res}"

    await engine.dispose()
