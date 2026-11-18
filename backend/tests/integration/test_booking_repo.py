"""Integration tests for booking repository queries and row locking against isolated test DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import (
    Appointment,
    BookingProposal,
    ConsentChallenge,
)
from app.repositories.booking_repo import (
    get_active_consent_challenge,
    get_appointment_by_id,
    get_appointment_by_id_for_update,
    get_proposal_by_id,
    get_proposal_by_id_for_update,
    get_proposal_by_idempotency_key,
)

pytestmark = pytest.mark.asyncio


async def test_proposal_crud_and_locks(test_db_session: AsyncSession):
    now = datetime.now(UTC)
    prop = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        proposal_type="new_booking",
        appointment_id=None,
        confirmed_appointment_id=None,
        expected_appointment_version=None,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id=f"sess-repo-{uuid4().hex[:8]}",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Repo Test Caller",
        caller_phone="+919876543210",
        caller_email="repo@example.com",
        start_at=now + timedelta(days=5),
        end_at=now + timedelta(days=5, minutes=30),
        blocked_until_at=now + timedelta(days=5, minutes=45),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key=f"idemp-{uuid4().hex}",
        created_at=now,
    )
    test_db_session.add(prop)
    await test_db_session.flush()

    # Query without lock
    fetched = await get_proposal_by_id(test_db_session, prop.id)
    assert fetched is not None
    assert fetched.id == prop.id
    assert fetched.service_name_snapshot == "General Consultation"

    # Query by idempotency key
    fetched_key = await get_proposal_by_idempotency_key(test_db_session, prop.idempotency_key)
    assert fetched_key is not None
    assert fetched_key.id == prop.id

    # Query for update
    async with test_db_session.begin():
        locked = await get_proposal_by_id_for_update(test_db_session, prop.id)
        assert locked is not None
        assert locked.id == prop.id


async def test_appointment_crud_and_locks(test_db_session: AsyncSession):
    now = datetime.now(UTC)
    apt = Appointment(
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Repo Apt Caller",
        caller_phone="+919876543210",
        caller_email="apt@example.com",
        start_at=now + timedelta(days=7),
        end_at=now + timedelta(days=7, minutes=30),
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=now + timedelta(days=7, minutes=45),
        status="confirmed",
        version=1,
        created_at=now,
        updated_at=now,
    )
    test_db_session.add(apt)
    await test_db_session.flush()

    # Query without lock
    fetched = await get_appointment_by_id(test_db_session, apt.id)
    assert fetched is not None
    assert fetched.id == apt.id

    # Query with lock
    async with test_db_session.begin():
        locked = await get_appointment_by_id_for_update(test_db_session, apt.id)
        assert locked is not None
        assert locked.id == apt.id


async def test_active_consent_challenge_query(test_db_session: AsyncSession):
    now = datetime.now(UTC)
    session_id = f"sess-ch-{uuid4().hex[:8]}"

    challenge = ConsentChallenge(
        challenge_id=uuid4(),
        session_id=session_id,
        clinic_id=1,
        action_type="create_booking",
        target_id="prop-123",
        target_version=None,
        readback_details_hash="hash-123",
        readback_completed_turn_id=1,
        status="pending",
        expires_at=now + timedelta(seconds=300),
        created_at=now,
    )
    test_db_session.add(challenge)
    await test_db_session.flush()

    active = await get_active_consent_challenge(test_db_session, session_id)
    assert active is not None
    assert active.challenge_id == challenge.challenge_id
    assert active.status == "pending"
