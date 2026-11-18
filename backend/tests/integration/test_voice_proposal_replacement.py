"""PostgreSQL regression for the one-active-challenge-per-call constraint."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.db.models.booking import ConsentChallenge
from app.services.voice_booking_service import VoiceBookingService


@pytest.mark.asyncio
async def test_replacing_proposal_invalidates_previous_challenge(test_db_session):
    now = datetime.now(UTC)
    session_id = f"sess-{uuid4().hex}"

    def challenge():
        return ConsentChallenge(
            challenge_id=uuid4(), session_id=session_id, clinic_id=1,
            action_type="create_booking", target_id=str(uuid4()),
            readback_details_hash="a" * 64, status="pending_readback",
            created_at=now, expires_at=now + timedelta(minutes=5),
        )

    old = challenge()
    test_db_session.add(old)
    await test_db_session.flush()
    await VoiceBookingService._lock_voice_session(test_db_session, session_id)
    await VoiceBookingService._replace_session_proposals(test_db_session, session_id, now)
    new = challenge()
    test_db_session.add(new)
    await test_db_session.flush()
    await test_db_session.refresh(old)
    assert old.status == "invalidated"
    assert old.invalidation_reason == "replaced"
    assert new.status == "pending_readback"
    await test_db_session.rollback()
