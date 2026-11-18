"""Unit tests for caller identity verification and verification session management."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.db.models.booking import Appointment, CallerVerificationSession
from app.schemas.dto import AppointmentDTO
from app.services.booking_errors import (
    InvalidInputError,
    VerificationRequiredError,
)
from app.services.verification import (
    CallerVerificationManager,
    VerificationInput,
    lookup_and_verify_appointment,
    verify_caller_credentials,
)

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def sample_appointment_dto() -> AppointmentDTO:
    now = datetime.now(UTC)
    start = datetime(2026, 11, 25, 10, 0, tzinfo=KOLKATA_TZ).astimezone(UTC)
    end = start + timedelta(minutes=30)
    blocked = end + timedelta(minutes=15)
    return AppointmentDTO(
        id=42,
        clinic_id=1,
        service_id=1,
        service_name="General Consultation",
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        start_at=start,
        end_at=end,
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=blocked,
        status="confirmed",
        version=1,
        cancellation_reason=None,
        created_at=now,
        updated_at=now,
    )


# =============================================================================
# Raw ASCII Validation & Credential Matching Tests
# =============================================================================


def test_verify_caller_credentials_valid_match(sample_appointment_dto):
    inp = VerificationInput(
        caller_name="rahul sharma",  # case-insensitive
        appointment_date=date(2026, 11, 25),
        caller_phone_last4="3210",
    )
    assert verify_caller_credentials(sample_appointment_dto, inp, KOLKATA_TZ)


def test_verify_caller_last4_strict_no_strip(sample_appointment_dto):
    # Leading whitespace must be rejected with InvalidInputError
    with pytest.raises(InvalidInputError, match="must be exactly four ASCII decimal digits"):
        verify_caller_credentials(
            sample_appointment_dto,
            VerificationInput(
                caller_name="Rahul Sharma",
                appointment_date=date(2026, 11, 25),
                caller_phone_last4=" 3210",
            ),
            KOLKATA_TZ,
        )

    # Trailing whitespace must be rejected with InvalidInputError
    with pytest.raises(InvalidInputError, match="must be exactly four ASCII decimal digits"):
        verify_caller_credentials(
            sample_appointment_dto,
            VerificationInput(
                caller_name="Rahul Sharma",
                appointment_date=date(2026, 11, 25),
                caller_phone_last4="3210 ",
            ),
            KOLKATA_TZ,
        )


def test_verify_caller_last4_unicode_rejected(sample_appointment_dto):
    with pytest.raises(InvalidInputError, match="must be exactly four ASCII decimal digits"):
        verify_caller_credentials(
            sample_appointment_dto,
            VerificationInput(
                caller_name="Rahul Sharma",
                appointment_date=date(2026, 11, 25),
                caller_phone_last4="३२१०",
            ),
            KOLKATA_TZ,
        )


def test_verify_caller_last4_non_digits_rejected(sample_appointment_dto):
    with pytest.raises(InvalidInputError, match="must be exactly four ASCII decimal digits"):
        verify_caller_credentials(
            sample_appointment_dto,
            VerificationInput(
                caller_name="Rahul Sharma",
                appointment_date=date(2026, 11, 25),
                caller_phone_last4="321a",
            ),
            KOLKATA_TZ,
        )


def test_verify_caller_last4_length_errors(sample_appointment_dto):
    with pytest.raises(InvalidInputError, match="must be exactly four ASCII decimal digits"):
        verify_caller_credentials(
            sample_appointment_dto,
            VerificationInput(
                caller_name="Rahul Sharma",
                appointment_date=date(2026, 11, 25),
                caller_phone_last4="321",
            ),
            KOLKATA_TZ,
        )
    with pytest.raises(InvalidInputError, match="must be exactly four ASCII decimal digits"):
        verify_caller_credentials(
            sample_appointment_dto,
            VerificationInput(
                caller_name="Rahul Sharma",
                appointment_date=date(2026, 11, 25),
                caller_phone_last4="32100",
            ),
            KOLKATA_TZ,
        )


def test_verify_caller_credentials_name_mismatch(sample_appointment_dto):
    inp = VerificationInput(
        caller_name="Amit Patel",
        appointment_date=date(2026, 11, 25),
        caller_phone_last4="3210",
    )
    assert not verify_caller_credentials(sample_appointment_dto, inp, KOLKATA_TZ)


def test_verify_caller_credentials_date_mismatch(sample_appointment_dto):
    inp = VerificationInput(
        caller_name="Rahul Sharma",
        appointment_date=date(2026, 11, 26),
        caller_phone_last4="3210",
    )
    assert not verify_caller_credentials(sample_appointment_dto, inp, KOLKATA_TZ)


# =============================================================================
# CallerVerificationManager Tests
# =============================================================================


@pytest.mark.asyncio
async def test_verification_manager_record_success_id_mismatch(sample_appointment_dto):
    manager = CallerVerificationManager()
    session = AsyncMock()

    # Pass appointment_id = 99 while DTO id = 42
    with pytest.raises(ValueError, match="Mismatched appointment_id"):
        await manager.record_success(
            session=session,
            session_id="sess-1",
            clinic_id=1,
            appointment_id=99,
            appointment=sample_appointment_dto,
        )


@pytest.mark.asyncio
async def test_verification_manager_lockout_after_max_attempts():
    manager = CallerVerificationManager(max_attempts=3)
    session = AsyncMock()

    now = datetime.now(UTC)
    fake_rec = CallerVerificationSession(
        session_id="sess-1",
        clinic_id=1,
        verification_failures=2,
        locked_out=False,
        created_at=now,
        updated_at=now,
    )

    exec_res = MagicMock()
    exec_res.scalar_one.return_value = fake_rec
    session.execute.return_value = exec_res

    # Attempt 3: reached threshold
    res = await manager.record_failure(session, "sess-1", 1)
    assert not res.success
    assert res.handoff_required
    assert fake_rec.locked_out
    assert "security" in res.advisory_message.lower()


@pytest.mark.asyncio
async def test_assert_verified_requires_matching_appointment():
    manager = CallerVerificationManager()
    session = AsyncMock()

    now = datetime.now(UTC)
    fake_rec = CallerVerificationSession(
        session_id="sess-1",
        clinic_id=1,
        verification_failures=0,
        locked_out=False,
        verified_appointment_id=42,
        verified_at=now,
        created_at=now,
        updated_at=now,
    )

    exec_res = MagicMock()
    exec_res.scalar_one_or_none.return_value = fake_rec
    session.execute.return_value = exec_res

    # Matching appointment 42 passes
    await manager.assert_verified(session, "sess-1", 42)

    # Mismatched appointment 99 raises VerificationRequiredError
    with pytest.raises(VerificationRequiredError, match="has not been verified"):
        await manager.assert_verified(session, "sess-1", 99)


@pytest.mark.asyncio
async def test_assert_verified_expired_after_ttl():
    manager = CallerVerificationManager(verification_ttl_minutes=15)
    session = AsyncMock()

    now = datetime.now(UTC)
    # verified_at was 20 minutes ago
    verified_at = now - timedelta(minutes=20)
    fake_rec = CallerVerificationSession(
        session_id="sess-1",
        clinic_id=1,
        verification_failures=0,
        locked_out=False,
        verified_appointment_id=42,
        verified_at=verified_at,
        created_at=verified_at,
        updated_at=verified_at,
    )

    exec_res = MagicMock()
    exec_res.scalar_one_or_none.return_value = fake_rec
    session.execute.return_value = exec_res

    with pytest.raises(VerificationRequiredError, match="has expired"):
        await manager.assert_verified(session, "sess-1", 42)


# =============================================================================
# Lookup and Verify Appointment Tests
# =============================================================================


@pytest.mark.asyncio
async def test_lookup_and_verify_appointment_no_match():
    manager = CallerVerificationManager(max_attempts=3)
    session = AsyncMock()

    now = datetime.now(UTC)
    fake_rec = CallerVerificationSession(
        session_id="sess-1",
        clinic_id=1,
        verification_failures=0,
        locked_out=False,
        created_at=now,
        updated_at=now,
    )

    # 1st call for get_or_create_session
    mock_lock_res = MagicMock()
    mock_lock_res.scalar_one.return_value = fake_rec

    # 2nd call for query candidates (empty)
    mock_candidates_res = MagicMock()
    mock_candidates_res.scalars.return_value.all.return_value = []

    session.execute.side_effect = [
        MagicMock(),
        mock_lock_res,
        mock_candidates_res,
        MagicMock(),
        mock_lock_res,
    ]

    res = await lookup_and_verify_appointment(
        session=session,
        verification_manager=manager,
        session_id="sess-1",
        clinic_id=1,
        verification=VerificationInput("Non Existent", date(2026, 11, 25), "9999"),
        clinic_tz=KOLKATA_TZ,
    )

    assert not res.success
    assert not res.handoff_required
    assert "could not find an appointment" in res.advisory_message


@pytest.mark.asyncio
async def test_lookup_and_verify_appointment_happy_path():
    manager = CallerVerificationManager()
    session = AsyncMock()

    now = datetime.now(UTC)
    fake_rec = CallerVerificationSession(
        session_id="sess-1",
        clinic_id=1,
        verification_failures=0,
        locked_out=False,
        created_at=now,
        updated_at=now,
    )

    start = datetime(2026, 11, 25, 10, 0, tzinfo=KOLKATA_TZ).astimezone(UTC)
    end = start + timedelta(minutes=30)
    blocked = end + timedelta(minutes=15)
    apt = Appointment(
        id=42,
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        start_at=start,
        end_at=end,
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=blocked,
        status="confirmed",
        version=1,
        created_at=now,
        updated_at=now,
    )

    mock_lock_res = MagicMock()
    mock_lock_res.scalar_one.return_value = fake_rec

    mock_candidates_res = MagicMock()
    mock_candidates_res.scalars.return_value.all.return_value = [apt]

    session.execute.side_effect = [
        MagicMock(),
        mock_lock_res,
        mock_candidates_res,
        MagicMock(),
        mock_lock_res,
    ]

    res = await lookup_and_verify_appointment(
        session=session,
        verification_manager=manager,
        session_id="sess-1",
        clinic_id=1,
        verification=VerificationInput("Rahul Sharma", date(2026, 11, 25), "3210"),
        clinic_tz=KOLKATA_TZ,
    )

    assert res.success
    assert res.appointment is not None
    assert res.appointment.id == 42
    assert fake_rec.verified_appointment_id == 42
