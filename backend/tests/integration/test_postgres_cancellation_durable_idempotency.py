"""Integration tests verifying durable table-wide idempotency for cancellation proposals."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.booking import Appointment, CallerVerificationSession, CancellationProposal
from app.services.voice_booking_service import IdempotencyKeyConsumedError, VoiceBookingService

pytestmark = pytest.mark.asyncio
TEST_SECRET = "voxora_test_secret_key_32_characters_long_min"


async def test_cancellation_idempotency_replays_active_proposal(test_db_url: str) -> None:
    """Repeated call with same idempotency_key returns identical pending proposal details."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    service = VoiceBookingService(session_factory=session_factory, signing_secret=TEST_SECRET)

    session_id = f"sess-idem-{uuid4().hex[:8]}"
    now_utc = datetime.now(UTC)
    shared_key = f"idem-key-{uuid4().hex[:8]}"

    async with session_factory() as session:
        apt = Appointment(
            clinic_id=1,
            service_id=1,
            service_name_snapshot="General Dental Checkup",
            caller_name="Priya Sharma",
            caller_phone="+919876543210",
            start_at=now_utc + timedelta(days=2),
            end_at=now_utc + timedelta(days=2, minutes=30),
            duration_minutes=30,
            buffer_minutes=15,
            blocked_until_at=now_utc + timedelta(days=2, minutes=45),
            status="confirmed",
            version=1,
        )
        session.add(apt)
        await session.flush()

        v_sess = CallerVerificationSession(
            session_id=session_id,
            clinic_id=1,
            verified_appointment_id=apt.id,
            verified_at=now_utc,
        )
        session.add(v_sess)
        await session.commit()

    # Call 1: creates proposal
    res1 = await service.create_cancellation_proposal(
        voice_session_id=session_id,
        reason_category="scheduling_conflict",
        current_turn_id=1,
        idempotency_key=shared_key,
    )
    assert res1["status"] == "pending_readback"

    # Call 2: in-flight replay returns identical proposal ID
    res2 = await service.create_cancellation_proposal(
        voice_session_id=session_id,
        reason_category="scheduling_conflict",
        current_turn_id=1,
        idempotency_key=shared_key,
    )
    assert res2["status"] == "pending_readback"
    assert res2["proposal_id"] == res1["proposal_id"]

    await engine.dispose()


async def test_cancellation_idempotency_consumed_key_raises_error(test_db_url: str) -> None:
    """Reusing an idempotency key associated with an expired or replaced proposal raises IdempotencyKeyConsumedError."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    service = VoiceBookingService(session_factory=session_factory, signing_secret=TEST_SECRET)

    session_id = f"sess-consumed-{uuid4().hex[:8]}"
    now_utc = datetime.now(UTC)
    consumed_key = f"idem-consumed-{uuid4().hex[:8]}"

    async with session_factory() as session:
        apt = Appointment(
            clinic_id=1,
            service_id=1,
            service_name_snapshot="General Dental Checkup",
            caller_name="Priya Sharma",
            caller_phone="+919876543210",
            start_at=now_utc + timedelta(days=2),
            end_at=now_utc + timedelta(days=2, minutes=30),
            duration_minutes=30,
            buffer_minutes=15,
            blocked_until_at=now_utc + timedelta(days=2, minutes=45),
            status="confirmed",
            version=1,
        )
        session.add(apt)
        await session.flush()

        v_sess = CallerVerificationSession(
            session_id=session_id,
            clinic_id=1,
            verified_appointment_id=apt.id,
            verified_at=now_utc,
        )
        session.add(v_sess)

        # Pre-seed an expired proposal with this idempotency key
        expired_prop = CancellationProposal(
            id=uuid4(),
            clinic_id=1,
            appointment_id=apt.id,
            expected_appointment_version=1,
            session_id=session_id,
            created_turn_id=1,
            caller_phone="+919876543210",
            reason_category="scheduling_conflict",
            state="expired",
            expires_at=now_utc - timedelta(minutes=1),
            idempotency_key=consumed_key,
        )
        session.add(expired_prop)
        await session.commit()

    with pytest.raises(IdempotencyKeyConsumedError):
        await service.create_cancellation_proposal(
            voice_session_id=session_id,
            reason_category="scheduling_conflict",
            current_turn_id=2,
            idempotency_key=consumed_key,
        )

    await engine.dispose()
