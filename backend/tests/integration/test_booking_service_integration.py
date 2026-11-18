"""Integration tests for state machine, snapshot insulation, and database check constraints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.booking import Appointment, BookingProposal
from app.db.models.clinic import Service
from app.services.consent import (
    ActionType,
    CallerIntent,
    CallerTurnEvent,
    ServerConsentCoordinator,
)

pytestmark = pytest.mark.asyncio

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")
VALID_SECRET = "voxora_test_secret_key_32_characters_long_min"


async def test_consent_challenge_persists_across_connections(test_db_url: str):
    """Prove that consent challenges persist in PostgreSQL and work across independent database connections."""
    engine1 = create_async_engine(test_db_url)
    engine2 = create_async_engine(test_db_url)
    maker1 = async_sessionmaker(bind=engine1, class_=AsyncSession, expire_on_commit=False)
    maker2 = async_sessionmaker(bind=engine2, class_=AsyncSession, expire_on_commit=False)

    coord = ServerConsentCoordinator(secret_key=VALID_SECRET)
    session_id = f"sess-cross-worker-{uuid4().hex[:8]}"
    prop_id = uuid4()

    # Connection 1 initiates challenge
    async with maker1() as s1:
        challenge = await coord.initiate_challenge(
            session=s1,
            voice_session_id=session_id,
            clinic_id=1,
            action_type=ActionType.CONFIRM_BOOKING,
            target_id=str(prop_id),
            readback_completed_turn_id=1,
            readback_text="General Consultation on Friday at 10 AM",
        )
        assert challenge.status == "pending"

    # Connection 2 receives caller turn event and processes affirmative confirmation
    turn_event = CallerTurnEvent(
        session_id=session_id,
        turn_id=2,
        intent=CallerIntent.AFFIRMATIVE_CONFIRMATION,
        raw_transcript="Yes, please book it.",
    )

    async with maker2() as s2:
        res = await coord.process_caller_turn(
            session=s2,
            event=turn_event,
            expected_action_type=ActionType.CONFIRM_BOOKING,
            expected_target_id=str(prop_id),
        )
        assert res.authorized
        assert res.authorization is not None
        assert res.authorization.session_id == session_id

    await engine1.dispose()
    await engine2.dispose()


async def test_proposal_state_linkage_constraint_violation(test_db_session: AsyncSession):
    """Verify that PostgreSQL enforces chk_proposal_state_linkage constraint."""
    now = datetime.now(UTC)
    # A proposal with state='confirmed' MUST have confirmed_appointment_id IS NOT NULL and consumed_at IS NOT NULL
    invalid_proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        proposal_type="new_booking",
        appointment_id=None,
        confirmed_appointment_id=None,  # Invalid: confirmed without appointment link!
        expected_appointment_version=None,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id=f"sess-chk-{uuid4().hex[:8]}",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Invalid Caller",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=3),
        end_at=now + timedelta(days=3, minutes=30),
        blocked_until_at=now + timedelta(days=3, minutes=45),
        state="confirmed",  # Violates constraint!
        expires_at=now + timedelta(minutes=15),
        idempotency_key=f"idemp-{uuid4().hex}",
        created_at=now,
        consumed_at=now,
    )

    test_db_session.add(invalid_proposal)
    with pytest.raises(IntegrityError):
        await test_db_session.flush()
    await test_db_session.rollback()


async def test_duration_and_buffer_snapshot_insulation(test_db_session: AsyncSession):
    """Verify historical appointments insulate against dynamic service configuration changes."""
    now = datetime.now(UTC)
    start_at = now + timedelta(days=14)

    # 1. Create appointment with 30m duration and 15m buffer
    apt = Appointment(
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Insulation Test Caller",
        caller_phone="+919876543210",
        caller_email=None,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=start_at + timedelta(minutes=45),
        status="confirmed",
        version=1,
        created_at=now,
        updated_at=now,
    )
    test_db_session.add(apt)
    await test_db_session.flush()

    # 2. Mutate the live service in DB to have 60m duration and 30m buffer
    svc = await test_db_session.get(Service, 1)
    orig_duration = svc.duration_minutes
    orig_buffer = svc.buffer_minutes

    try:
        svc.duration_minutes = 60
        svc.buffer_minutes = 30
        await test_db_session.flush()

        # 3. Reload appointment and verify its persisted snapshots remain unchanged
        await test_db_session.refresh(apt)
        assert apt.duration_minutes == 30
        assert apt.buffer_minutes == 15
        assert apt.blocked_until_at == start_at + timedelta(minutes=45)
    finally:
        # Restore service
        svc.duration_minutes = orig_duration
        svc.buffer_minutes = orig_buffer
        await test_db_session.flush()
