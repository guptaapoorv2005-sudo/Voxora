"""Integration tests verifying handoff gating and invalidation of pending challenges."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.booking import Appointment, CallerVerificationSession, ConsentChallenge
from app.services.voice_booking_service import SessionHandedOffError, VoiceBookingService

pytestmark = pytest.mark.asyncio
TEST_SECRET = "voxora_test_secret_key_32_characters_long_min"


async def test_handoff_invalidates_challenges_and_blocks_mutations(test_db_url: str) -> None:
    """Handoff sets handed_off=True, invalidates active challenges, and blocks subsequent proposals."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    service = VoiceBookingService(session_factory=session_factory, signing_secret=TEST_SECRET)

    session_id = f"sess-handoff-{uuid4().hex[:8]}"
    now_utc = datetime.now(UTC)
    cid = uuid4()

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
            handed_off=False,
        )
        session.add(v_sess)

        chal = ConsentChallenge(
            challenge_id=cid,
            session_id=session_id,
            clinic_id=1,
            action_type="confirm_cancellation",
            target_id=str(uuid4()),
            readback_details_hash="b" * 64,
            readback_completed_turn_id=1,
            readback_completed_at=now_utc - timedelta(seconds=5),
            status="pending",
            expires_at=now_utc + timedelta(minutes=5),
        )
        session.add(chal)
        await session.commit()

    # Trigger human handoff
    handoff_script = await service.request_human_handoff(
        voice_session_id=session_id,
        category="caller_requested",
    )
    assert "demonstration system" in handoff_script

    # Verify session is marked handed_off and challenge is invalidated
    async with session_factory() as check_session:
        v_sess_row = await check_session.get(CallerVerificationSession, session_id)
        assert v_sess_row is not None
        assert v_sess_row.handed_off is True

        chal_row = await check_session.get(ConsentChallenge, cid)
        assert chal_row is not None
        assert chal_row.status == "invalidated"
        assert chal_row.invalidation_reason == "handoff_caller_requested"

    # Subsequent operation on handed-off session fails closed with SessionHandedOffError
    with pytest.raises(SessionHandedOffError):
        await service.create_cancellation_proposal(
            voice_session_id=session_id,
            reason_category="scheduling_conflict",
            current_turn_id=2,
            idempotency_key=f"idem-{uuid4().hex[:8]}",
        )

    await engine.dispose()
