"""Integration tests verifying client-isolated step-up failure reset semantics."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.booking import Appointment, AppointmentVerificationFailure
from app.services.voice_booking_service import VoiceBookingService

pytestmark = pytest.mark.asyncio
TEST_SECRET = "voxora_test_secret_key_32_characters_long_min"


async def test_successful_step_up_clears_only_own_failures(test_db_url: str) -> None:
    """Successful step-up verification resets failures for the verifying client, preserving other clients' failures."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    service = VoiceBookingService(session_factory=session_factory, signing_secret=TEST_SECRET)

    legitimate_client_id = f"client-legit-{uuid4().hex[:8]}"
    attacker_client_id = f"client-attacker-{uuid4().hex[:8]}"
    session_id = f"sess-stepup-{uuid4().hex[:8]}"
    now_utc = datetime.now(UTC)

    async with session_factory() as session:
        apt = Appointment(
            clinic_id=1,
            service_id=1,
            service_name_snapshot="General Dental Checkup",
            caller_name="Priya Sharma",
            caller_phone="+919876543210",
            start_at=now_utc + timedelta(days=4),
            end_at=now_utc + timedelta(days=4, minutes=30),
            duration_minutes=30,
            buffer_minutes=15,
            blocked_until_at=now_utc + timedelta(days=4, minutes=45),
            status="confirmed",
            version=1,
        )
        session.add(apt)
        await session.flush()
        apt_id = apt.id

        # Insert failures for both legitimate client and attacker
        session.add(
            AppointmentVerificationFailure(
                appointment_id=apt_id,
                client_id=legitimate_client_id,
                attempted_at=now_utc - timedelta(minutes=5),
            )
        )
        session.add(
            AppointmentVerificationFailure(
                appointment_id=apt_id,
                client_id=attacker_client_id,
                attempted_at=now_utc - timedelta(minutes=4),
            )
        )
        await session.commit()

    # Legitimate client verifies with full 10-digit phone number
    res = await service.verify_appointment_step_up(
        voice_session_id=session_id,
        client_id=legitimate_client_id,
        appointment_id=apt_id,
        caller_name="Priya Sharma",
        full_phone="+919876543210",
    )
    assert res["status"] == "verified"

    # Verify that only legitimate client's failure was cleared
    async with session_factory() as check_session:
        legit_count = (
            await check_session.execute(
                select(func.count(AppointmentVerificationFailure.id)).where(
                    AppointmentVerificationFailure.appointment_id == apt_id,
                    AppointmentVerificationFailure.client_id == legitimate_client_id,
                )
            )
        ).scalar()
        attacker_count = (
            await check_session.execute(
                select(func.count(AppointmentVerificationFailure.id)).where(
                    AppointmentVerificationFailure.appointment_id == apt_id,
                    AppointmentVerificationFailure.client_id == attacker_client_id,
                )
            )
        ).scalar()

        assert legit_count == 0, "Legitimate client's failure was not cleared upon success"
        assert attacker_count == 1, "Attacker client's failure was incorrectly deleted"

    await engine.dispose()
