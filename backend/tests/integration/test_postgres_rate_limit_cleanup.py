"""Integration tests verifying truthful operational retention and PII cleanup."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.booking import (
    BookingProposal,
    CallerVerificationSession,
    ClientVerificationAttempt,
    TokenIssuanceAttempt,
)

pytestmark = pytest.mark.asyncio


async def test_retention_cleanup_purges_old_records_and_redacts_pii(test_db_url: str) -> None:
    """Retention cleanup query purges records > 24 hours old and redacts proposals > 30 days old."""
    engine = create_async_engine(test_db_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    now_utc = datetime.now(UTC)
    old_25h = now_utc - timedelta(hours=25)
    recent_1h = now_utc - timedelta(hours=1)
    old_31d = now_utc - timedelta(days=31)
    recent_5d = now_utc - timedelta(days=5)

    client_id = f"client-clean-{uuid4().hex[:8]}"
    old_sid = f"sess-old-{uuid4().hex[:8]}"
    recent_sid = f"sess-recent-{uuid4().hex[:8]}"

    old_booking_id = uuid4()
    recent_booking_id = uuid4()

    async with session_factory() as session:
        # 1. Rate-limiting attempts
        session.add(
            ClientVerificationAttempt(
                client_id=client_id,
                attempt_type="standard",
                attempted_at=old_25h,
            )
        )
        session.add(
            ClientVerificationAttempt(
                client_id=client_id,
                attempt_type="standard",
                attempted_at=recent_1h,
            )
        )
        session.add(
            TokenIssuanceAttempt(
                client_ip_hash="a1b2c3d4e5f60718",
                attempted_at=old_25h,
            )
        )

        # 2. Sessions
        session.add(
            CallerVerificationSession(
                session_id=old_sid,
                clinic_id=1,
                updated_at=old_25h,
            )
        )
        session.add(
            CallerVerificationSession(
                session_id=recent_sid,
                clinic_id=1,
                updated_at=recent_1h,
            )
        )

        # 3. Proposals for redaction
        session.add(
            BookingProposal(
                id=old_booking_id,
                clinic_id=1,
                service_id=1,
                service_name_snapshot="Checkup",
                proposal_type="new_booking",
                session_id=old_sid,
                created_turn_id=1,
                duration_minutes=30,
                buffer_minutes=15,
                caller_name="Priya Sharma",
                caller_phone="+919876543210",
                caller_email="priya@example.com",
                start_at=old_31d,
                end_at=old_31d + timedelta(minutes=30),
                blocked_until_at=old_31d + timedelta(minutes=45),
                state="confirmed",
                expires_at=old_31d + timedelta(minutes=5),
                idempotency_key=f"idem-{uuid4().hex[:8]}",
                created_at=old_31d,
            )
        )
        session.add(
            BookingProposal(
                id=recent_booking_id,
                clinic_id=1,
                service_id=1,
                service_name_snapshot="Checkup",
                proposal_type="new_booking",
                session_id=recent_sid,
                created_turn_id=1,
                duration_minutes=30,
                buffer_minutes=15,
                caller_name="Priya Sharma",
                caller_phone="+919876543210",
                caller_email="priya@example.com",
                start_at=recent_5d,
                end_at=recent_5d + timedelta(minutes=30),
                blocked_until_at=recent_5d + timedelta(minutes=45),
                state="confirmed",
                expires_at=recent_5d + timedelta(minutes=5),
                idempotency_key=f"idem-{uuid4().hex[:8]}",
                created_at=recent_5d,
            )
        )
        await session.commit()

    # Execute retention cleanup SQL
    async with session_factory() as session:
        await session.execute(
            text(
                "DELETE FROM client_verification_attempts WHERE attempted_at < NOW() - INTERVAL '24 hours';"
            )
        )
        await session.execute(
            text(
                "DELETE FROM appointment_verification_failures WHERE attempted_at < NOW() - INTERVAL '24 hours';"
            )
        )
        await session.execute(
            text(
                "DELETE FROM token_issuance_attempts WHERE attempted_at < NOW() - INTERVAL '24 hours';"
            )
        )
        await session.execute(
            text(
                "DELETE FROM caller_verification_sessions WHERE updated_at < NOW() - INTERVAL '24 hours';"
            )
        )
        await session.execute(
            text("""
            UPDATE booking_proposals 
            SET caller_phone = 'REDACTED', caller_email = NULL 
            WHERE created_at < NOW() - INTERVAL '30 days' AND caller_phone != 'REDACTED';
        """)
        )
        await session.commit()

    # Verify assertions
    async with session_factory() as verify_session:
        # Check attempts purged
        remaining_attempts = (
            await verify_session.execute(
                select(func.count(ClientVerificationAttempt.id)).where(
                    ClientVerificationAttempt.client_id == client_id
                )
            )
        ).scalar()
        assert remaining_attempts == 1, "Old verification attempt was not purged"

        # Check session purged
        assert (await verify_session.get(CallerVerificationSession, old_sid)) is None
        assert (await verify_session.get(CallerVerificationSession, recent_sid)) is not None

        # Check proposal PII redacted
        old_prop = await verify_session.get(BookingProposal, old_booking_id)
        assert old_prop is not None
        assert old_prop.caller_phone == "REDACTED"
        assert old_prop.caller_email is None

        recent_prop = await verify_session.get(BookingProposal, recent_booking_id)
        assert recent_prop is not None
        assert recent_prop.caller_phone == "+919876543210"
        assert recent_prop.caller_email == "priya@example.com"

    await engine.dispose()
