"""Integration tests verifying single-transaction atomic consent validation and booking mutation."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.booking import (
    Appointment,
    BookingProposal,
    ConsentChallenge,
)
from app.services.booking_errors import (
    InvalidConsentError,
)
from app.services.voice_booking_service import VoiceBookingService

pytestmark = pytest.mark.asyncio


async def test_atomic_consent_and_booking_confirmation(test_db_url: str) -> None:
    """Validate that confirm_proposal atomically validates consent, mutates booking, and consumes challenge."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    service = VoiceBookingService(session_factory=session_factory, clinic_id=1)
    session_id = f"sess-atomic-{uuid4().hex[:8]}"
    prop_id = uuid4()
    cid = uuid4()
    now_utc = datetime.now(UTC)
    expires_at = now_utc + timedelta(minutes=5)

    # 1. Setup pending proposal and challenge in pending status
    async with session_factory() as session:
        challenge = ConsentChallenge(
            challenge_id=cid,
            session_id=session_id,
            clinic_id=1,
            action_type="create_booking",
            target_id=str(prop_id),
            readback_details_hash="a" * 64,
            readback_completed_turn_id=1,
            readback_completed_at=now_utc - timedelta(seconds=10),
            status="pending",
            expires_at=expires_at,
        )
        session.add(challenge)
        await session.flush()

        proposal = BookingProposal(
            id=prop_id,
            clinic_id=1,
            service_id=1,
            service_name_snapshot="General Consultation",
            proposal_type="new_booking",
            session_id=session_id,
            created_turn_id=1,
            duration_minutes=30,
            buffer_minutes=15,
            caller_name="Priya Sharma",
            caller_phone="+919876543210",
            start_at=now_utc + timedelta(days=2),
            end_at=now_utc + timedelta(days=2, minutes=30),
            blocked_until_at=now_utc + timedelta(days=2, minutes=45),
            state="pending",
            expires_at=expires_at,
            idempotency_key=f"idem-{uuid4().hex[:8]}",
            challenge_id=cid,
            readback_details_hash="a" * 64,
        )
        session.add(proposal)
        await session.commit()

    # 2. Confirm proposal with affirmative turn
    res = await service.confirm_proposal(
        proposal_id_str=str(prop_id),
        voice_session_id=session_id,
        affirmative_turn_id=2,
        affirmative_item_id="item-msg-user-affirm",
        affirmative_transcript="Yes, please go ahead and book it.",
        speech_started_at=now_utc,
        is_cancellation=False,
    )

    assert res["status"] == "confirmed"
    apt_id = res["appointment_id"]
    assert apt_id is not None

    # 3. Verify database state
    async with session_factory() as session:
        # Challenge consumed
        chal = await session.get(ConsentChallenge, cid)
        assert chal is not None
        assert chal.status == "consumed"
        assert chal.consumed_turn_id == 2
        assert chal.consumed_item_id == "item-msg-user-affirm"

        # Proposal confirmed
        prop = await session.get(BookingProposal, prop_id)
        assert prop is not None
        assert prop.state == "confirmed"
        assert prop.confirmed_appointment_id == apt_id

        # Appointment created
        apt = await session.get(Appointment, apt_id)
        assert apt is not None
        assert apt.status == "confirmed"
        assert apt.caller_name == "Priya Sharma"

    # 4. Idempotent retry returns same confirmed appointment
    res_retry = await service.confirm_proposal(
        proposal_id_str=str(prop_id),
        voice_session_id=session_id,
        affirmative_turn_id=2,
        affirmative_item_id="item-msg-user-affirm",
        affirmative_transcript="Yes, please go ahead and book it.",
        speech_started_at=now_utc,
        is_cancellation=False,
    )
    assert res_retry["status"] == "confirmed"
    assert res_retry["appointment_id"] == apt_id

    # 5. Non-matching session is rejected
    with pytest.raises(InvalidConsentError, match="does not belong"):
        await service.confirm_proposal(
            proposal_id_str=str(prop_id),
            voice_session_id="wrong-session-id",
            affirmative_turn_id=3,
            affirmative_item_id="item-msg-user-affirm-2",
            affirmative_transcript="Yes confirm",
            speech_started_at=now_utc,
            is_cancellation=False,
        )

    await engine.dispose()
