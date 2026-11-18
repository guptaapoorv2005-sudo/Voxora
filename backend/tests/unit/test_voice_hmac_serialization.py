"""Unit tests for Phase 5 canonical 11-field HMAC serialization and verification."""

import uuid
from datetime import UTC, datetime, timedelta

from app.services.consent_crypto import (
    compute_canonical_consent_payload,
    compute_phase5_consent_signature,
    verify_phase5_consent_signature,
)

SECRET = "super_secure_secret_key_minimum_32_chars_long_123"
OLD_SECRET = "previous_secret_key_rotation_valid_32_chars"


def test_canonical_payload_alphabetical_order():
    cid = str(uuid.uuid4())
    now = datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC)
    exp = now + timedelta(minutes=5)

    payload = compute_canonical_consent_payload(
        action_type="create_booking",
        challenge_id=cid,
        consumed_item_id="item-123",
        consumed_turn_id=3,
        created_at=now,
        expires_at=exp,
        nonce="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        readback_details_hash="0000111122223333444455556666777788889999aaaabbbbccccddddeeeeffff",
        session_id="session-xyz",
        target_id="target-789",
        target_version=1,
    )

    pairs = payload.split("&")
    keys = [p.split("=")[0] for p in pairs]
    assert keys == sorted(keys), "Keys in payload must be strictly sorted alphabetically"
    assert len(keys) == 11


def test_compute_and_verify_signature_success():
    cid = str(uuid.uuid4())
    now = datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC)
    exp = now + timedelta(minutes=5)

    sig = compute_phase5_consent_signature(
        secret_key=SECRET,
        action_type="reschedule_appointment",
        challenge_id=cid,
        consumed_item_id="item-abc",
        consumed_turn_id=5,
        created_at=now,
        expires_at=exp,
        nonce="11223344556677889900aabbccddeeff",
        readback_details_hash="deadbeef" * 8,
        session_id="session-reschedule",
        target_id="target-100",
        target_version=2,
    )

    assert isinstance(sig, str)
    assert len(sig) == 64

    is_valid = verify_phase5_consent_signature(
        signature=sig,
        secret_key=SECRET,
        action_type="reschedule_appointment",
        challenge_id=cid,
        consumed_item_id="item-abc",
        consumed_turn_id=5,
        created_at=now,
        expires_at=exp,
        nonce="11223344556677889900aabbccddeeff",
        readback_details_hash="deadbeef" * 8,
        session_id="session-reschedule",
        target_id="target-100",
        target_version=2,
    )
    assert is_valid is True


def test_tampering_fails_verification():
    cid = str(uuid.uuid4())
    now = datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC)
    exp = now + timedelta(minutes=5)

    sig = compute_phase5_consent_signature(
        secret_key=SECRET,
        action_type="cancel_appointment",
        challenge_id=cid,
        consumed_item_id="item-cancel",
        consumed_turn_id=2,
        created_at=now,
        expires_at=exp,
        nonce="ffff0000ffff0000ffff0000ffff0000",
        readback_details_hash="cafe" * 16,
        session_id="session-cancel",
        target_id="target-200",
        target_version=None,
    )

    # Tampering with target_id
    assert (
        verify_phase5_consent_signature(
            signature=sig,
            secret_key=SECRET,
            action_type="cancel_appointment",
            challenge_id=cid,
            consumed_item_id="item-cancel",
            consumed_turn_id=2,
            created_at=now,
            expires_at=exp,
            nonce="ffff0000ffff0000ffff0000ffff0000",
            readback_details_hash="cafe" * 16,
            session_id="session-cancel",
            target_id="target-TAMPERED",
            target_version=None,
        )
        is False
    )


def test_key_rotation_fallback():
    cid = str(uuid.uuid4())
    now = datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC)
    exp = now + timedelta(minutes=5)

    sig = compute_phase5_consent_signature(
        secret_key=OLD_SECRET,
        action_type="create_booking",
        challenge_id=cid,
        consumed_item_id="item-1",
        consumed_turn_id=1,
        created_at=now,
        expires_at=exp,
        nonce="12341234123412341234123412341234",
        readback_details_hash="feed" * 16,
        session_id="session-1",
        target_id="target-1",
        target_version=None,
    )

    # Verification with new SECRET fails without previous_secret_keys
    assert (
        verify_phase5_consent_signature(
            signature=sig,
            secret_key=SECRET,
            action_type="create_booking",
            challenge_id=cid,
            consumed_item_id="item-1",
            consumed_turn_id=1,
            created_at=now,
            expires_at=exp,
            nonce="12341234123412341234123412341234",
            readback_details_hash="feed" * 16,
            session_id="session-1",
            target_id="target-1",
            target_version=None,
        )
        is False
    )

    # Succeeds when OLD_SECRET is supplied in previous_secret_keys
    assert (
        verify_phase5_consent_signature(
            signature=sig,
            secret_key=SECRET,
            action_type="create_booking",
            challenge_id=cid,
            consumed_item_id="item-1",
            consumed_turn_id=1,
            created_at=now,
            expires_at=exp,
            nonce="12341234123412341234123412341234",
            readback_details_hash="feed" * 16,
            session_id="session-1",
            target_id="target-1",
            target_version=None,
            previous_secret_keys=[OLD_SECRET],
        )
        is True
    )
