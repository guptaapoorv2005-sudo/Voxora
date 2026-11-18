"""Integration tests verifying server-derived appointment identity and IDOR elimination."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.booking import Appointment, CallerVerificationSession
from app.services.voice_booking_service import UnverifiedCallerSessionError, VoiceBookingService

pytestmark = pytest.mark.asyncio
TEST_SECRET = "voxora_test_secret_key_32_characters_long_min"


async def test_reschedule_proposal_fails_closed_when_session_unverified(test_db_url: str) -> None:
    """create_reschedule_proposal raises UnverifiedCallerSessionError if caller is not verified."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    service = VoiceBookingService(session_factory=session_factory, signing_secret=TEST_SECRET)

    unverified_session_id = f"sess-unverified-{uuid4().hex[:8]}"

    with pytest.raises(UnverifiedCallerSessionError):
        await service.create_reschedule_proposal(
            voice_session_id=unverified_session_id,
            target_start_iso="2026-11-20T10:00:00+05:30",
            current_turn_id=1,
            idempotency_key=f"idem-{uuid4().hex[:8]}",
        )

    await engine.dispose()


async def test_cancellation_proposal_fails_closed_when_session_unverified(test_db_url: str) -> None:
    """create_cancellation_proposal raises UnverifiedCallerSessionError if caller is not verified."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    service = VoiceBookingService(session_factory=session_factory, signing_secret=TEST_SECRET)

    unverified_session_id = f"sess-unverified-{uuid4().hex[:8]}"

    with pytest.raises(UnverifiedCallerSessionError):
        await service.create_cancellation_proposal(
            voice_session_id=unverified_session_id,
            reason_category="patient_request",
            current_turn_id=1,
            idempotency_key=f"idem-{uuid4().hex[:8]}",
        )

    await engine.dispose()


async def test_proposals_strictly_derive_appointment_from_verified_session(
    test_db_url: str,
) -> None:
    """Proposals strictly derive target appointment ID from session state under lock."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    service = VoiceBookingService(session_factory=session_factory, signing_secret=TEST_SECRET)

    verified_session_id = f"sess-verified-{uuid4().hex[:8]}"
    now_utc = datetime.now(UTC)

    async with session_factory() as session:
        # Create appointment
        apt = Appointment(
            clinic_id=1,
            service_id=1,
            service_name_snapshot="General Dental Checkup",
            caller_name="Priya Sharma",
            caller_phone="+919876543210",
            start_at=now_utc + timedelta(days=3),
            end_at=now_utc + timedelta(days=3, minutes=30),
            duration_minutes=30,
            buffer_minutes=15,
            blocked_until_at=now_utc + timedelta(days=3, minutes=45),
            status="confirmed",
            version=1,
        )
        session.add(apt)
        await session.flush()

        # Create verified session row bound to apt.id
        v_sess = CallerVerificationSession(
            session_id=verified_session_id,
            clinic_id=1,
            verified_appointment_id=apt.id,
            verified_at=now_utc,
        )
        session.add(v_sess)
        await session.commit()
        target_apt_id = apt.id

    # Create cancellation proposal: caller supplies NO appointment ID!
    res = await service.create_cancellation_proposal(
        voice_session_id=verified_session_id,
        reason_category="scheduling_conflict",
        current_turn_id=1,
        idempotency_key=f"idem-{uuid4().hex[:8]}",
    )

    assert res["status"] == "pending_readback"
    # Target appointment derived was exactly the verified appointment ID
    assert res["appointment_id"] == target_apt_id

    await engine.dispose()
