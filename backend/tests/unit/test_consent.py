"""Unit tests for server consent coordination, HMAC signature verification, and intent handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.db.models.booking import ConsentChallenge
from app.schemas.dto import ConsentAuthorization
from app.services.consent import (
    ActionType,
    CallerIntent,
    CallerTurnEvent,
    ServerConsentCoordinator,
    compute_consent_signature,
    verify_consent_signature,
)

VALID_SECRET = "voxora_test_secret_key_32_characters_long_min"


def test_server_coordinator_key_length_validation():
    with pytest.raises(ValueError, match="at least 32 characters"):
        ServerConsentCoordinator(secret_key="too_short")


def test_canonical_json_hmac_no_delimiter_collision():
    now = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
    expires = now + timedelta(seconds=300)
    sig1 = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="session:1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id="target:a",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires,
        ttl_seconds=300,
        expected_target_version=None,
    )

    # A distinct session_id with different delimiters cannot collide
    sig2 = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="session_1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id="target_a",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires,
        ttl_seconds=300,
        expected_target_version=None,
    )
    assert sig1 != sig2


def test_hmac_signature_tampered_ttl_rejected():
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=300)
    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id="prop-1",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires,
        ttl_seconds=300,
        expected_target_version=None,
    )

    # Tampering with ttl_seconds
    tampered = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id="prop-1",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires,
        ttl_seconds=600,  # Tampered!
        signature=sig,
        expected_target_version=None,
    )
    assert not verify_consent_signature(tampered, VALID_SECRET, now)


def test_hmac_signature_tampered_expires_at_rejected():
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=300)
    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id="prop-1",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires,
        ttl_seconds=300,
        expected_target_version=None,
    )

    # Tampering with expires_at
    tampered = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id="prop-1",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires + timedelta(seconds=60),  # Tampered!
        ttl_seconds=300,
        signature=sig,
        expected_target_version=None,
    )
    assert not verify_consent_signature(tampered, VALID_SECRET, now)


def test_hmac_signature_future_clock_skew_rejected():
    now = datetime.now(UTC)
    # created_at is 45s in future (allowed skew is max 30s)
    future_time = now + timedelta(seconds=45)
    expires = future_time + timedelta(seconds=300)
    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id="prop-1",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=future_time,
        expires_at=expires,
        ttl_seconds=300,
        expected_target_version=None,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id="prop-1",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=future_time,
        expires_at=expires,
        ttl_seconds=300,
        signature=sig,
        expected_target_version=None,
    )
    assert not verify_consent_signature(consent, VALID_SECRET, now)


def test_hmac_signature_excessive_ttl_rejected():
    now = datetime.now(UTC)
    # TTL > 600 raises ValueError during computation
    with pytest.raises(ValueError, match="between 1 and 600"):
        compute_consent_signature(
            secret_key=VALID_SECRET,
            session_id="sess-1",
            action_type=ActionType.CONFIRM_BOOKING,
            target_id="prop-1",
            authorized_turn_id=2,
            challenge_id="ch-1",
            nonce="nonce-1",
            created_at=now,
            expires_at=now + timedelta(seconds=900),
            ttl_seconds=900,
            expected_target_version=None,
        )


def test_hmac_signature_exact_boundary_expiry():
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=300)
    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id="prop-1",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires,
        ttl_seconds=300,
        expected_target_version=None,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id="prop-1",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires,
        ttl_seconds=300,
        signature=sig,
        expected_target_version=None,
    )

    # At now == expires: REJECTED as expired
    assert not verify_consent_signature(consent, VALID_SECRET, expires)
    # At now == expires - 1 microsecond: ACCEPTED
    assert verify_consent_signature(consent, VALID_SECRET, expires - timedelta(microseconds=1))


@pytest.mark.asyncio
async def test_coordinator_initiate_challenge():
    coordinator = ServerConsentCoordinator(VALID_SECRET)
    session = AsyncMock()

    challenge = await coordinator.initiate_challenge(
        session=session,
        session_id="test-session-1",
        clinic_id=1,
        action_type=ActionType.CONFIRM_BOOKING,
        target_id="prop-uuid-1",
        target_version=None,
        readback_summary="Service: Teeth Cleaning, Date: 2026-11-25 10:00 AM",
        turn_id=1,
    )
    assert challenge.session_id == "test-session-1"
    assert challenge.readback_completed_turn_id == 1
    assert challenge.status == "pending"
    assert session.add.called
    assert session.flush.called


@pytest.mark.asyncio
async def test_coordinator_process_caller_turn_affirmative():
    coordinator = ServerConsentCoordinator(VALID_SECRET)
    session = AsyncMock()

    now = datetime.now(UTC)
    fake_challenge = ConsentChallenge(
        challenge_id=uuid4(),
        session_id="test-session-1",
        clinic_id=1,
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id="prop-uuid-1",
        target_version=None,
        readback_details_hash="hash123",
        readback_completed_turn_id=1,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
    )

    exec_res = MagicMock()
    exec_res.scalar_one_or_none.return_value = fake_challenge
    session.execute.return_value = exec_res

    turn_event = CallerTurnEvent(
        session_id="test-session-1",
        turn_id=2,  # turn 2 > turn 1
        intent=CallerIntent.AFFIRMATIVE,
        raw_utterance="Yes, please confirm that booking.",
        timestamp=now,
    )

    auth = await coordinator.process_caller_turn(session, "test-session-1", turn_event)
    assert auth is not None
    assert auth.session_id == "test-session-1"
    assert auth.action_type == ActionType.CONFIRM_BOOKING.value
    assert auth.authorized_turn_id == 2
    assert fake_challenge.status == "consumed"
    assert verify_consent_signature(auth, VALID_SECRET, now)


@pytest.mark.asyncio
async def test_coordinator_process_caller_turn_inquiry_does_not_authorize():
    coordinator = ServerConsentCoordinator(VALID_SECRET)
    session = AsyncMock()

    now = datetime.now(UTC)
    fake_challenge = ConsentChallenge(
        challenge_id=uuid4(),
        session_id="test-session-1",
        clinic_id=1,
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id="prop-uuid-1",
        target_version=None,
        readback_details_hash="hash123",
        readback_completed_turn_id=1,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
    )

    exec_res = MagicMock()
    exec_res.scalar_one_or_none.return_value = fake_challenge
    session.execute.return_value = exec_res

    turn_event = CallerTurnEvent(
        session_id="test-session-1",
        turn_id=2,
        intent=CallerIntent.INQUIRY,
        raw_utterance="Is parking available at the clinic?",
        timestamp=now,
    )

    auth = await coordinator.process_caller_turn(session, "test-session-1", turn_event)
    assert auth is None
    assert fake_challenge.status == "pending"  # remains pending for follow-up


@pytest.mark.asyncio
async def test_coordinator_process_caller_turn_negative_invalidates():
    coordinator = ServerConsentCoordinator(VALID_SECRET)
    session = AsyncMock()

    now = datetime.now(UTC)
    fake_challenge = ConsentChallenge(
        challenge_id=uuid4(),
        session_id="test-session-1",
        clinic_id=1,
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id="prop-uuid-1",
        target_version=None,
        readback_details_hash="hash123",
        readback_completed_turn_id=1,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
    )

    exec_res = MagicMock()
    exec_res.scalar_one_or_none.return_value = fake_challenge
    session.execute.return_value = exec_res

    turn_event = CallerTurnEvent(
        session_id="test-session-1",
        turn_id=2,
        intent=CallerIntent.NEGATIVE,
        raw_utterance="No, wait, I want a different time.",
        timestamp=now,
    )

    auth = await coordinator.process_caller_turn(session, "test-session-1", turn_event)
    assert auth is None
    assert fake_challenge.status == "invalidated"
