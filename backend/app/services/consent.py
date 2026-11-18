"""Server-side affirmative consent management and cryptographic authorization.

Implements the proven consent challenge lifecycle, turn-based intent validation,
canonical JSON HMAC-SHA256 signature generation, and verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import ConsentChallenge
from app.schemas.dto import ConsentAuthorization

MAX_CLOCK_SKEW_SECONDS = 30
MAX_CONSENT_TTL_SECONDS = 600


class ActionType(StrEnum):
    """Mutating booking actions requiring affirmative caller consent."""

    CONFIRM_BOOKING = "confirm_booking"
    CONFIRM_RESCHEDULE = "confirm_reschedule"
    CANCEL_APPOINTMENT = "cancel_appointment"


class CallerIntent(StrEnum):
    """Server-classified caller utterance intent."""

    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    INQUIRY = "inquiry"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CallerTurnEvent:
    """Authoritative caller turn event from the server conversation layer."""

    session_id: str
    turn_id: int
    intent: CallerIntent
    raw_utterance: str
    timestamp: datetime


def compute_consent_signature(
    *,
    secret_key: str,
    session_id: str,
    action_type: ActionType,
    target_id: str,
    authorized_turn_id: int,
    challenge_id: str,
    nonce: str,
    created_at: datetime,
    expires_at: datetime,
    ttl_seconds: int,
    expected_target_version: int | None,
) -> str:
    """Compute an unforgeable HMAC-SHA256 signature over a canonical JSON payload."""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware UTC datetime.")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expires_at must be timezone-aware UTC datetime.")
    if ttl_seconds <= 0 or ttl_seconds > MAX_CONSENT_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be between 1 and {MAX_CONSENT_TTL_SECONDS}.")
    if expires_at != created_at + timedelta(seconds=ttl_seconds):
        raise ValueError("expires_at must exactly match created_at + ttl_seconds.")

    payload = {
        "action_type": action_type.value,
        "authorized_turn_id": authorized_turn_id,
        "challenge_id": challenge_id,
        "created_at_utc": created_at.astimezone(UTC).isoformat(),
        "expected_target_version": expected_target_version,
        "expires_at_utc": expires_at.astimezone(UTC).isoformat(),
        "nonce": nonce,
        "session_id": session_id,
        "target_id": target_id,
        "ttl_seconds": ttl_seconds,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret_key.encode("utf-8"), canonical_json.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_consent_signature(
    consent: ConsentAuthorization,
    secret_key: str,
    now_utc: datetime,
) -> bool:
    """Verify constant-time HMAC signature, clock skew, and boundary expiry."""
    # 1. Clock skew check
    if consent.created_at > now_utc + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        return False
    # 2. TTL bounds
    if consent.ttl_seconds <= 0 or consent.ttl_seconds > MAX_CONSENT_TTL_SECONDS:
        return False
    if consent.expires_at != consent.created_at + timedelta(seconds=consent.ttl_seconds):
        return False
    # 3. Exact boundary expiry check
    if now_utc >= consent.expires_at:
        return False

    try:
        expected_sig = compute_consent_signature(
            secret_key=secret_key,
            session_id=consent.session_id,
            action_type=ActionType(consent.action_type),
            target_id=consent.target_id,
            authorized_turn_id=consent.authorized_turn_id,
            challenge_id=consent.challenge_id,
            nonce=consent.nonce,
            created_at=consent.created_at,
            expires_at=consent.expires_at,
            ttl_seconds=consent.ttl_seconds,
            expected_target_version=consent.expected_target_version,
        )
    except (ValueError, KeyError):
        return False

    return hmac.compare_digest(consent.signature, expected_sig)


class ServerConsentCoordinator:
    """Trusted server coordinator managing consent challenges and issuing authorizations."""

    def __init__(self, secret_key: str, default_ttl_seconds: int = 300, max_ttl_seconds: int = 600):
        if len(secret_key) < 32:
            raise ValueError("secret_key must be at least 32 characters.")
        self._secret_key = secret_key
        self._default_ttl_seconds = default_ttl_seconds
        self._max_ttl_seconds = max_ttl_seconds

    async def initiate_challenge(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        clinic_id: int,
        action_type: ActionType,
        target_id: str,
        target_version: int | None,
        readback_summary: str,
        turn_id: int,
    ) -> ConsentChallenge:
        """Record an active consent challenge, invalidating any existing active challenge for the session."""
        now_utc = datetime.now(UTC)
        readback_hash = hashlib.sha256(readback_summary.strip().lower().encode("utf-8")).hexdigest()

        # Invalidate any existing active challenge for this session (explicit replacement)
        await session.execute(
            update(ConsentChallenge)
            .where(ConsentChallenge.session_id == session_id, ConsentChallenge.status == "pending")
            .values(status="invalidated", invalidated_at=now_utc, invalidation_reason="replaced")
        )

        challenge = ConsentChallenge(
            challenge_id=uuid4(),
            session_id=session_id,
            clinic_id=clinic_id,
            action_type=action_type.value,
            target_id=target_id,
            target_version=target_version,
            readback_details_hash=readback_hash,
            readback_completed_turn_id=turn_id,
            status="pending",
            created_at=now_utc,
            expires_at=now_utc + timedelta(seconds=self._default_ttl_seconds),
        )
        session.add(challenge)
        await session.flush()
        return challenge

    async def process_caller_turn(
        self,
        session: AsyncSession,
        session_id: str,
        turn_event: CallerTurnEvent,
    ) -> ConsentAuthorization | None:
        """Process an authoritative caller turn event under PostgreSQL row lock."""
        now_utc = datetime.now(UTC)

        res = await session.execute(
            select(ConsentChallenge)
            .where(ConsentChallenge.session_id == session_id, ConsentChallenge.status == "pending")
            .with_for_update()
        )
        challenge = res.scalar_one_or_none()
        if challenge is None:
            return None

        if now_utc >= challenge.expires_at:
            challenge.status = "invalidated"
            challenge.invalidated_at = now_utc
            challenge.invalidation_reason = "expired"
            await session.flush()
            return None

        # Turn sequencing: affirmative turn must follow readback completion turn
        if challenge.readback_completed_turn_id is None or turn_event.turn_id <= challenge.readback_completed_turn_id:
            return None

        # Intent evaluation
        if turn_event.intent != CallerIntent.AFFIRMATIVE:
            if turn_event.intent == CallerIntent.NEGATIVE:
                challenge.status = "invalidated"
                challenge.invalidated_at = now_utc
                challenge.invalidation_reason = "caller_rejected"
                await session.flush()
            # Inquiries and ambiguous responses leave challenge pending without issuing authorization
            return None

        # Atomic consumption
        challenge.status = "consumed"
        challenge.consumed_turn_id = turn_event.turn_id
        challenge.consumed_at = now_utc

        nonce = str(uuid4())  # Server-generated nonce
        expires_at = now_utc + timedelta(seconds=self._default_ttl_seconds)

        sig = compute_consent_signature(
            secret_key=self._secret_key,
            session_id=challenge.session_id,
            action_type=ActionType(challenge.action_type),
            target_id=challenge.target_id,
            authorized_turn_id=turn_event.turn_id,
            challenge_id=str(challenge.challenge_id),
            nonce=nonce,
            created_at=now_utc,
            expires_at=expires_at,
            ttl_seconds=self._default_ttl_seconds,
            expected_target_version=challenge.target_version,
        )
        await session.flush()

        return ConsentAuthorization(
            session_id=challenge.session_id,
            action_type=challenge.action_type,
            target_id=challenge.target_id,
            authorized_turn_id=turn_event.turn_id,
            challenge_id=str(challenge.challenge_id),
            nonce=nonce,
            created_at=now_utc,
            expires_at=expires_at,
            ttl_seconds=self._default_ttl_seconds,
            signature=sig,
            expected_target_version=challenge.target_version,
        )
