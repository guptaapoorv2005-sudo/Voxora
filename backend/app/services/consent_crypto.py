"""Cryptographic canonical payload serialization and HMAC-SHA256 signature verification.

Implements the 11-field URL-encoded canonical payload and signature verification for Phase 5.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from datetime import UTC, datetime


def compute_canonical_consent_payload(
    *,
    action_type: str,
    challenge_id: str,
    consumed_item_id: str,
    consumed_turn_id: int,
    created_at: datetime,
    expires_at: datetime,
    nonce: str,
    readback_details_hash: str,
    session_id: str,
    target_id: str,
    target_version: int | None,
) -> str:
    """Serialize the canonical 11 fields alphabetically via urllib.parse.urlencode."""
    fields = [
        ("action_type", action_type),
        ("challenge_id", challenge_id),
        ("consumed_item_id", consumed_item_id),
        ("consumed_turn_id", str(consumed_turn_id)),
        ("created_at", created_at.astimezone(UTC).isoformat()),
        ("expires_at", expires_at.astimezone(UTC).isoformat()),
        ("nonce", nonce),
        ("readback_details_hash", readback_details_hash),
        ("session_id", session_id),
        ("target_id", target_id),
        ("target_version", str(target_version) if target_version is not None else ""),
    ]
    fields.sort(key=lambda x: x[0])
    return urllib.parse.urlencode(fields)


def compute_phase5_consent_signature(
    *,
    secret_key: str,
    action_type: str,
    challenge_id: str,
    consumed_item_id: str,
    consumed_turn_id: int,
    created_at: datetime,
    expires_at: datetime,
    nonce: str,
    readback_details_hash: str,
    session_id: str,
    target_id: str,
    target_version: int | None,
) -> str:
    """Compute HMAC-SHA256 signature over the canonical 11-field URL-encoded payload."""
    payload = compute_canonical_consent_payload(
        action_type=action_type,
        challenge_id=challenge_id,
        consumed_item_id=consumed_item_id,
        consumed_turn_id=consumed_turn_id,
        created_at=created_at,
        expires_at=expires_at,
        nonce=nonce,
        readback_details_hash=readback_details_hash,
        session_id=session_id,
        target_id=target_id,
        target_version=target_version,
    )
    return hmac.new(
        secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_phase5_consent_signature(
    *,
    signature: str,
    secret_key: str,
    action_type: str,
    challenge_id: str,
    consumed_item_id: str,
    consumed_turn_id: int,
    created_at: datetime,
    expires_at: datetime,
    nonce: str,
    readback_details_hash: str,
    session_id: str,
    target_id: str,
    target_version: int | None,
    previous_secret_keys: list[str] | None = None,
) -> bool:
    """Verify HMAC signature with constant-time comparison, supporting key rotation."""
    keys_to_try = [secret_key] + (previous_secret_keys or [])
    for key in keys_to_try:
        expected = compute_phase5_consent_signature(
            secret_key=key,
            action_type=action_type,
            challenge_id=challenge_id,
            consumed_item_id=consumed_item_id,
            consumed_turn_id=consumed_turn_id,
            created_at=created_at,
            expires_at=expires_at,
            nonce=nonce,
            readback_details_hash=readback_details_hash,
            session_id=session_id,
            target_id=target_id,
            target_version=target_version,
        )
        if hmac.compare_digest(signature, expected):
            return True
    return False
