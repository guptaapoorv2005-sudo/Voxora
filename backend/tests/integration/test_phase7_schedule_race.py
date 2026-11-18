"""Race schedule edits against a confirmation-style validation in PostgreSQL."""

import asyncio
import secrets
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.clinic import BusinessClosure, BusinessHours, BusinessProfile, Service
from app.services.availability import acquire_calendar_advisory_locks
from app.services.booking_errors import SlotUnavailableError
from app.services.schedule_validation import lock_schedule_configuration, validate_schedule_slot


@pytest.mark.integration
async def test_closure_commit_serializes_with_confirmation(test_engine, test_db_url):
    schema = "phase7_schedule_" + secrets.token_hex(8)
    async with test_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_db_url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            for model in (BusinessProfile, BusinessHours, BusinessClosure, Service):
                await conn.run_sync(lambda c, table=model.__table__: table.create(c))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tz = ZoneInfo("Asia/Kolkata")
        day = datetime.now(tz).date() + timedelta(days=14)
        start = datetime.combine(day, time(10), tzinfo=tz).astimezone(UTC)
        async with factory() as db:
            profile = BusinessProfile(
                slug="brightsmile",
                name="Test",
                description="Test",
                address_line1="Test",
                city="Test",
                state="Test",
                postal_code="1",
                country="India",
                timezone="Asia/Kolkata",
            )
            db.add(profile)
            await db.flush()
            clinic_id = profile.id
            db.add_all(
                [
                    BusinessHours(
                        clinic_id=clinic_id,
                        day_of_week=day.weekday(),
                        is_closed=False,
                        open_time=time(9),
                        close_time=time(18),
                    ),
                    Service(
                        id=1,
                        clinic_id=clinic_id,
                        name="Checkup",
                        duration_minutes=30,
                        buffer_minutes=15,
                        price_inr=Decimal("500"),
                        min_lead_time_hours=2,
                        cancellation_window_hours=24,
                        is_active=True,
                        sort_order=0,
                    ),
                ]
            )
            await db.commit()
        locked = asyncio.Event()
        release = asyncio.Event()
        validating = asyncio.Event()

        async def edit():
            async with factory.begin() as db:
                await lock_schedule_configuration(db, clinic_id)
                db.add(
                    BusinessClosure(clinic_id=clinic_id, date=day, reason="Closed", all_day=True)
                )
                await db.flush()
                locked.set()
                await release.wait()

        async def confirm():
            async with factory.begin() as db:
                await acquire_calendar_advisory_locks(
                    db, clinic_id, start, start + timedelta(minutes=45), tz
                )
                validating.set()
                await lock_schedule_configuration(db, clinic_id)
                await validate_schedule_slot(
                    db,
                    clinic_id=clinic_id,
                    service_id=1,
                    start_at=start,
                    duration_minutes=30,
                    buffer_minutes=15,
                    service_name="Checkup",
                    now=datetime.now(UTC),
                )

        writer = asyncio.create_task(edit())
        await asyncio.wait_for(locked.wait(), 5)
        reader = asyncio.create_task(confirm())
        try:
            await asyncio.wait_for(validating.wait(), 5)
            await asyncio.sleep(0.1)
            assert not reader.done(), "Confirmation must wait for the schedule edit"
        finally:
            release.set()
        await asyncio.wait_for(writer, 5)
        with pytest.raises(SlotUnavailableError):
            await asyncio.wait_for(reader, 5)
    finally:
        await engine.dispose()
        async with test_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
