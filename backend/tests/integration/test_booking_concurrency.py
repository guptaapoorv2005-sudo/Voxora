"""PostgreSQL concurrency integration tests: advisory lock serialization, race recovery, and atomicity."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.schemas.dto import ConsentAuthorization
from app.services.booking import BookingService
from app.services.booking_errors import (
    SlotUnavailableError,
)
from app.services.consent import ActionType, compute_consent_signature
from app.services.verification import CallerVerificationManager

pytestmark = pytest.mark.asyncio

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")
VALID_SECRET = "voxora_test_secret_key_32_characters_long_min"


async def test_concurrent_confirm_different_proposals_same_empty_slot(test_db_url: str):
    """Prove Level 3 advisory lock serializes concurrent confirmations for the same slot.

    Two distinct voice callers receive proposals for the exact same slot.
    Both confirm concurrently.
    PostgreSQL advisory lock ensures one caller wins, while the other is rejected
    with SlotUnavailableError under lock.
    """
    engine1 = create_async_engine(test_db_url)
    engine2 = create_async_engine(test_db_url)
    maker1 = async_sessionmaker(bind=engine1, class_=AsyncSession, expire_on_commit=False)
    maker2 = async_sessionmaker(bind=engine2, class_=AsyncSession, expire_on_commit=False)

    now = datetime.now(UTC)
    start_at = now + timedelta(days=10)
    service = BookingService()

    # Step 1: Create Proposal 1 (Caller A) and Proposal 2 (Caller B)
    async with maker1() as s1:
        p1 = await service.propose_booking(
            session=s1,
            clinic_id=1,
            service_id=1,
            session_id="session-caller-a",
            created_turn_id=1,
            caller_name="Caller A",
            caller_phone="+919876543210",
            caller_email=None,
            start_at=start_at,
        )

    async with maker2() as s2:
        p2 = await service.propose_booking(
            session=s2,
            clinic_id=1,
            service_id=1,
            session_id="session-caller-b",
            created_turn_id=1,
            caller_name="Caller B",
            caller_phone="+919876543211",
            caller_email=None,
            start_at=start_at,
        )

    # Prepare signed consent authorizations for both
    expires_at = now + timedelta(seconds=300)
    sig1 = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="session-caller-a",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id=str(p1.id),
        authorized_turn_id=2,
        challenge_id="ch-a",
        nonce=f"nonce-a-{uuid4().hex}",
        created_at=now,
        expires_at=expires_at,
        ttl_seconds=300,
        expected_target_version=None,
    )
    consent1 = ConsentAuthorization(
        session_id="session-caller-a",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(p1.id),
        authorized_turn_id=2,
        challenge_id="ch-a",
        nonce=f"nonce-a-{uuid4().hex}",
        created_at=now,
        expires_at=expires_at,
        ttl_seconds=300,
        signature=sig1,
    )

    sig2 = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="session-caller-b",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id=str(p2.id),
        authorized_turn_id=2,
        challenge_id="ch-b",
        nonce=f"nonce-b-{uuid4().hex}",
        created_at=now,
        expires_at=expires_at,
        ttl_seconds=300,
        expected_target_version=None,
    )
    consent2 = ConsentAuthorization(
        session_id="session-caller-b",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(p2.id),
        authorized_turn_id=2,
        challenge_id="ch-b",
        nonce=f"nonce-b-{uuid4().hex}",
        created_at=now,
        expires_at=expires_at,
        ttl_seconds=300,
        signature=sig2,
    )

    # Step 2: Concurrently confirm both proposals
    async def try_confirm_1():
        async with maker1() as s1:
            return await service.confirm_booking(
                session=s1,
                proposal_id=p1.id,
                voice_session_id="session-caller-a",
                consent=consent1,
                secret_key=VALID_SECRET,
                clinic_tz=KOLKATA_TZ,
            )

    async def try_confirm_2():
        async with maker2() as s2:
            return await service.confirm_booking(
                session=s2,
                proposal_id=p2.id,
                voice_session_id="session-caller-b",
                consent=consent2,
                secret_key=VALID_SECRET,
                clinic_tz=KOLKATA_TZ,
            )

    results = await asyncio.gather(
        try_confirm_1(),
        try_confirm_2(),
        return_exceptions=True,
    )

    # Exactly one must succeed, and exactly one must fail with SlotUnavailableError
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}"
    assert isinstance(failures[0], SlotUnavailableError)

    await engine1.dispose()
    await engine2.dispose()


async def test_concurrent_propose_booking_identical_race_recovery(test_db_url: str):
    """Prove identical proposal creation race recovery against real PostgreSQL constraints."""
    engine1 = create_async_engine(test_db_url)
    engine2 = create_async_engine(test_db_url)
    maker1 = async_sessionmaker(bind=engine1, class_=AsyncSession, expire_on_commit=False)
    maker2 = async_sessionmaker(bind=engine2, class_=AsyncSession, expire_on_commit=False)

    service = BookingService()
    now = datetime.now(UTC)
    start_at = now + timedelta(days=12)

    async def task_propose(maker, name):
        async with maker() as s:
            return await service.propose_booking(
                session=s,
                clinic_id=1,
                service_id=1,
                session_id="concurrent-sess",
                created_turn_id=1,
                caller_name="Same Caller",
                caller_phone="+919876543210",
                caller_email="same@example.com",
                start_at=start_at,
            )

    results = await asyncio.gather(
        task_propose(maker1, "t1"),
        task_propose(maker2, "t2"),
        return_exceptions=True,
    )

    # Both tasks must return ProposalDTO pointing to the exact same proposal ID!
    assert not any(isinstance(r, Exception) for r in results), f"Errors: {results}"
    p1, p2 = results
    assert p1.id == p2.id
    assert p1.idempotency_key == p2.idempotency_key

    await engine1.dispose()
    await engine2.dispose()


async def test_concurrent_first_time_verification_session_creation(test_db_url: str):
    """Prove upsert pattern handles concurrent session creation without primary key collisions."""
    engine1 = create_async_engine(test_db_url)
    engine2 = create_async_engine(test_db_url)
    maker1 = async_sessionmaker(bind=engine1, class_=AsyncSession, expire_on_commit=False)
    maker2 = async_sessionmaker(bind=engine2, class_=AsyncSession, expire_on_commit=False)

    vm = CallerVerificationManager()
    target_session_id = f"sess-first-{uuid4().hex[:8]}"

    async def create_session_task(maker):
        async with maker() as s:
            async with s.begin():
                return await vm.get_or_create_session(s, target_session_id, clinic_id=1)

    r1, r2 = await asyncio.gather(
        create_session_task(maker1),
        create_session_task(maker2),
    )

    assert r1.session_id == target_session_id
    assert r2.session_id == target_session_id

    await engine1.dispose()
    await engine2.dispose()
