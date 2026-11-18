"""Unit tests for BookingService state transitions, concurrency safety, and transaction ownership."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import InvalidRequestError

from app.db.models.booking import Appointment, BookingProposal
from app.db.models.clinic import Service
from app.schemas.dto import ConsentAuthorization
from app.services.booking import (
    BookingService,
    generate_proposal_idempotency_key,
    resolve_existing_proposal,
)
from app.services.booking_errors import (
    CancellationWindowError,
    InvalidConfirmationTurnError,
    InvalidConsentError,
    ProposalAlreadyUsedError,
    ProposalExpiredError,
    SessionMismatchError,
    SlotUnavailableError,
    StaleAppointmentVersionError,
    VerificationRequiredError,
)
from app.services.consent import ActionType, compute_consent_signature
from app.services.verification import CallerVerificationManager

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")
VALID_SECRET = "voxora_test_secret_key_32_characters_long_min"


def make_clean_session() -> AsyncMock:
    """Helper to return an AsyncMock session with in_transaction() returning False and begin() returning async context manager."""
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=False)
    session.begin = MagicMock(return_value=AsyncMock())
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def mock_service() -> Service:
    return Service(
        id=1,
        clinic_id=1,
        name="General Consultation",
        description="Routine checkup",
        duration_minutes=30,
        buffer_minutes=15,
        price_inr=Decimal("500.00"),
        min_lead_time_hours=2,
        cancellation_window_hours=24,
        sort_order=1,
    )


# =============================================================================
# Idempotency Key & Resolution Tests
# =============================================================================


def test_generate_proposal_idempotency_key_15_fields():
    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    key1 = generate_proposal_idempotency_key(
        clinic_id=1,
        proposal_type="new_booking",
        appointment_id=None,
        expected_appointment_version=None,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        duration_minutes=30,
        buffer_minutes=15,
        start_at=now,
    )
    assert isinstance(key1, str) and len(key1) == 64

    # Differing service_name_snapshot produces differing key
    key2 = generate_proposal_idempotency_key(
        clinic_id=1,
        proposal_type="new_booking",
        appointment_id=None,
        expected_appointment_version=None,
        service_id=1,
        service_name_snapshot="General Dental Consultation",
        session_id="sess-1",
        created_turn_id=1,
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        duration_minutes=30,
        buffer_minutes=15,
        start_at=now,
    )
    assert key1 != key2


def test_generate_proposal_idempotency_key_rejects_naive_start_at():
    naive_dt = datetime(2026, 11, 25, 10, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        generate_proposal_idempotency_key(
            clinic_id=1,
            proposal_type="new_booking",
            appointment_id=None,
            expected_appointment_version=None,
            service_id=1,
            service_name_snapshot="General Consultation",
            session_id="sess-1",
            created_turn_id=1,
            caller_name="Rahul Sharma",
            caller_phone="+919876543210",
            caller_email=None,
            duration_minutes=30,
            buffer_minutes=15,
            start_at=naive_dt,
        )


def test_generate_proposal_idempotency_key_rejects_non_canonical_phone():
    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="canonical E.164"):
        generate_proposal_idempotency_key(
            clinic_id=1,
            proposal_type="new_booking",
            appointment_id=None,
            expected_appointment_version=None,
            service_id=1,
            service_name_snapshot="General Consultation",
            session_id="sess-1",
            created_turn_id=1,
            caller_name="Rahul Sharma",
            caller_phone="9876543210",  # Missing +91
            caller_email=None,
            duration_minutes=30,
            buffer_minutes=15,
            start_at=now,
        )


@pytest.mark.asyncio
async def test_resolve_existing_proposal_exact_match_returns_dto():
    now = datetime.now(UTC)
    start = now + timedelta(days=2)
    end = start + timedelta(minutes=30)
    expires = now + timedelta(minutes=15)

    existing = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        proposal_type="new_booking",
        appointment_id=None,
        confirmed_appointment_id=None,
        expected_appointment_version=None,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        duration_minutes=30,
        buffer_minutes=15,
        start_at=start,
        end_at=end,
        blocked_until_at=end + timedelta(minutes=15),
        state="pending",
        expires_at=expires,
        idempotency_key="test-hash",
        created_at=now,
    )

    expected = {
        "clinic_id": 1,
        "proposal_type": "new_booking",
        "appointment_id": None,
        "expected_appointment_version": None,
        "service_id": 1,
        "service_name_snapshot": "General Consultation",
        "session_id": "sess-1",
        "created_turn_id": 1,
        "caller_name": "rahul sharma",
        "caller_phone": "+919876543210",
        "caller_email": "rahul@example.com",
        "duration_minutes": 30,
        "buffer_minutes": 15,
        "start_at": start,
        "end_at": end,
    }

    action = await resolve_existing_proposal(make_clean_session(), existing, expected)
    assert action[0] == "return_dto"
    assert action[1].id == existing.id


@pytest.mark.asyncio
async def test_resolve_existing_proposal_mismatch_raises_conflict():
    now = datetime.now(UTC)
    start = now + timedelta(days=2)
    end = start + timedelta(minutes=30)

    existing = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        proposal_type="new_booking",
        appointment_id=None,
        confirmed_appointment_id=None,
        expected_appointment_version=None,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email=None,
        duration_minutes=30,
        buffer_minutes=15,
        start_at=start,
        end_at=end,
        blocked_until_at=end + timedelta(minutes=15),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="test-hash",
        created_at=now,
    )

    expected = {
        "clinic_id": 1,
        "proposal_type": "new_booking",
        "appointment_id": None,
        "expected_appointment_version": None,
        "service_id": 2,  # Differing service_id
        "service_name_snapshot": "General Consultation",
        "session_id": "sess-1",
        "created_turn_id": 1,
        "caller_name": "Rahul Sharma",
        "caller_phone": "+919876543210",
        "caller_email": None,
        "duration_minutes": 30,
        "buffer_minutes": 15,
        "start_at": start,
        "end_at": end,
    }

    action = await resolve_existing_proposal(make_clean_session(), existing, expected)
    assert action[0] == "raise_conflict"


# =============================================================================
# Propose Booking Tests
# =============================================================================


@pytest.mark.asyncio
async def test_propose_booking_happy_path(mock_service):
    service = BookingService()
    session = make_clean_session()

    exec_svc = MagicMock()
    exec_svc.scalar_one_or_none.return_value = mock_service

    exec_key = MagicMock()
    exec_key.scalar_one_or_none.return_value = None  # No existing proposal

    session.execute.side_effect = [exec_svc, exec_key]

    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    dto = await service.propose_booking(
        session=session,
        clinic_id=1,
        service_id=1,
        session_id="sess-1",
        created_turn_id=1,
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        start_at=now,
    )

    assert dto.service_id == 1
    assert dto.service_name == "General Consultation"
    assert dto.state == "pending"
    assert session.add.called
    assert session.flush.called


@pytest.mark.asyncio
async def test_service_rejects_dirty_session():
    service = BookingService()
    dirty_session = make_clean_session()
    dirty_session.in_transaction = MagicMock(return_value=True)

    with pytest.raises(InvalidRequestError, match="requires a clean AsyncSession"):
        await service.propose_booking(
            session=dirty_session,
            clinic_id=1,
            service_id=1,
            session_id="sess-1",
            created_turn_id=1,
            caller_name="Rahul Sharma",
            caller_phone="+919876543210",
            caller_email=None,
            start_at=datetime.now(UTC),
        )


# =============================================================================
# Confirm Booking Tests
# =============================================================================


@pytest.mark.asyncio
async def test_confirm_booking_session_mismatch_rejected():
    service = BookingService()
    session = make_clean_session()

    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="original-session",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=datetime.now(UTC),
        end_at=datetime.now(UTC) + timedelta(minutes=30),
        blocked_until_at=datetime.now(UTC) + timedelta(minutes=45),
        state="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        idempotency_key="k1",
    )

    mock_exec = MagicMock()
    mock_exec.scalar_one_or_none.return_value = proposal
    session.execute.return_value = mock_exec

    consent = ConsentAuthorization(
        session_id="attacker-session",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=300),
        ttl_seconds=300,
        signature="dummy",
    )

    with pytest.raises(SessionMismatchError, match="does not belong to this voice session"):
        await service.confirm_booking(
            session=session,
            proposal_id=proposal.id,
            voice_session_id="attacker-session",
            consent=consent,
            secret_key=VALID_SECRET,
            clinic_tz=KOLKATA_TZ,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_confirm_booking_happy_path(monkeypatch, allowed):
    monkeypatch.setattr("app.services.booking.lock_schedule_configuration", AsyncMock())
    validate = AsyncMock(side_effect=None if allowed else SlotUnavailableError("Schedule changed"))
    monkeypatch.setattr("app.services.booking.validate_schedule_slot", validate)
    service = BookingService()
    session = make_clean_session()

    now = datetime.now(UTC)
    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        blocked_until_at=now + timedelta(days=2, minutes=45),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="k1",
        created_at=now,
    )

    mock_prop_exec = MagicMock()
    mock_prop_exec.scalar_one_or_none.return_value = proposal

    mock_conflict_exec = MagicMock()
    mock_conflict_exec.scalar_one_or_none.return_value = None  # No conflict

    session.execute.side_effect = [mock_prop_exec, MagicMock(), mock_conflict_exec]

    expires_at = now + timedelta(seconds=300)
    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires_at,
        ttl_seconds=300,
        expected_target_version=None,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=expires_at,
        ttl_seconds=300,
        signature=sig,
    )

    async def confirm():
        return await service.confirm_booking(
            session=session,
            proposal_id=proposal.id,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            clinic_tz=KOLKATA_TZ,
        )

    if not allowed:
        with pytest.raises(SlotUnavailableError):
            await confirm()
        assert proposal.state == "rejected"
        session.add.assert_not_called()
        validate.assert_awaited_once()
        return

    dto = await confirm()

    assert dto.service_id == 1
    assert dto.service_name == "General Consultation"
    assert dto.status == "confirmed"
    assert proposal.state == "confirmed"
    assert session.add.called
    assert session.flush.called
    validate.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_booking_expired_proposal_rejected():
    service = BookingService()
    session = make_clean_session()

    now = datetime.now(UTC)
    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        blocked_until_at=now + timedelta(days=2, minutes=45),
        state="pending",
        expires_at=now - timedelta(seconds=10),  # Already expired
        idempotency_key="k1",
        created_at=now - timedelta(minutes=20),
    )

    mock_exec = MagicMock()
    mock_exec.scalar_one_or_none.return_value = proposal
    session.execute.return_value = mock_exec

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature="sig",
    )

    with pytest.raises(ProposalExpiredError, match="has expired"):
        await service.confirm_booking(
            session=session,
            proposal_id=proposal.id,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            clinic_tz=KOLKATA_TZ,
        )


@pytest.mark.asyncio
async def test_confirm_booking_invalid_signature_rejected():
    service = BookingService()
    session = make_clean_session()

    now = datetime.now(UTC)
    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        blocked_until_at=now + timedelta(days=2, minutes=45),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="k1",
        created_at=now,
    )

    mock_exec = MagicMock()
    mock_exec.scalar_one_or_none.return_value = proposal
    session.execute.return_value = mock_exec

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature="invalid_signature",
    )

    with pytest.raises(InvalidConsentError, match="signature is invalid"):
        await service.confirm_booking(
            session=session,
            proposal_id=proposal.id,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            clinic_tz=KOLKATA_TZ,
        )


@pytest.mark.asyncio
async def test_confirm_booking_turn_order_rejected():
    service = BookingService()
    session = make_clean_session()

    now = datetime.now(UTC)
    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=5,  # Proposal created at turn 5
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        blocked_until_at=now + timedelta(days=2, minutes=45),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="k1",
        created_at=now,
    )

    mock_exec = MagicMock()
    mock_exec.scalar_one_or_none.return_value = proposal
    session.execute.return_value = mock_exec

    # Consent authorized at turn 5 (not strictly > 5)
    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id=str(proposal.id),
        authorized_turn_id=5,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        expected_target_version=None,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(proposal.id),
        authorized_turn_id=5,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature=sig,
    )

    with pytest.raises(InvalidConfirmationTurnError, match="must be strictly greater"):
        await service.confirm_booking(
            session=session,
            proposal_id=proposal.id,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            clinic_tz=KOLKATA_TZ,
        )


@pytest.mark.asyncio
async def test_confirm_booking_slot_conflict_rejected(monkeypatch):
    monkeypatch.setattr("app.services.booking.lock_schedule_configuration", AsyncMock())
    monkeypatch.setattr("app.services.booking.validate_schedule_slot", AsyncMock())
    service = BookingService()
    session = make_clean_session()

    now = datetime.now(UTC)
    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        blocked_until_at=now + timedelta(days=2, minutes=45),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="k1",
        created_at=now,
    )

    mock_prop_exec = MagicMock()
    mock_prop_exec.scalar_one_or_none.return_value = proposal

    mock_conflict_exec = MagicMock()
    mock_conflict_exec.scalar_one_or_none.return_value = Appointment(
        id=99,
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Other",
        caller_phone="+919999999999",
        start_at=proposal.start_at,
        end_at=proposal.end_at,
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=proposal.blocked_until_at,
        status="confirmed",
        version=1,
        created_at=now,
        updated_at=now,
    )

    session.execute.side_effect = [mock_prop_exec, MagicMock(), mock_conflict_exec]

    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        expected_target_version=None,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature=sig,
    )

    with pytest.raises(SlotUnavailableError, match="is no longer available"):
        await service.confirm_booking(
            session=session,
            proposal_id=proposal.id,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            clinic_tz=KOLKATA_TZ,
        )


# =============================================================================
# Reschedule & Cancellation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_propose_reschedule_requires_verification():
    service = BookingService()
    session = make_clean_session()

    verification_manager = MagicMock(spec=CallerVerificationManager)
    verification_manager.assert_verified = AsyncMock(
        side_effect=VerificationRequiredError("Caller identity has not been verified.")
    )

    with pytest.raises(VerificationRequiredError, match="has not been verified"):
        await service.propose_reschedule(
            session=session,
            verification_manager=verification_manager,
            appointment_id=42,
            clinic_id=1,
            service_id=1,
            session_id="sess-1",
            created_turn_id=1,
            start_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_cancel_appointment_stale_version_rejected():
    service = BookingService()
    session = make_clean_session()

    verification_manager = MagicMock(spec=CallerVerificationManager)
    verification_manager.assert_verified = AsyncMock(return_value="+919876543210")

    now = datetime.now(UTC)
    apt = Appointment(
        id=42,
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=now + timedelta(days=2, minutes=45),
        status="confirmed",
        version=2,  # Appointment version was incremented to 2!
        created_at=now,
        updated_at=now,
    )

    mock_apt_exec = MagicMock()
    mock_apt_exec.scalar_one_or_none.return_value = apt
    session.execute.return_value = mock_apt_exec

    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CANCEL_APPOINTMENT,
        target_id="42",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        expected_target_version=1,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CANCEL_APPOINTMENT.value,
        target_id="42",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature=sig,
        expected_target_version=1,
    )

    with pytest.raises(
        StaleAppointmentVersionError, match="was modified after cancellation consent"
    ):
        await service.cancel_appointment(
            session=session,
            verification_manager=verification_manager,
            appointment_id=42,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            reason="Change of plans",
            clinic_tz=KOLKATA_TZ,
        )


@pytest.mark.asyncio
async def test_cancel_appointment_cancellation_window_rejected():
    service = BookingService()
    session = make_clean_session()

    verification_manager = MagicMock(spec=CallerVerificationManager)
    verification_manager.assert_verified = AsyncMock(return_value="+919876543210")

    now = datetime.now(UTC)
    apt = Appointment(
        id=42,
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(hours=2),
        end_at=now + timedelta(hours=2, minutes=30),
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=now + timedelta(hours=2, minutes=45),
        status="confirmed",
        version=1,
        created_at=now,
        updated_at=now,
    )

    mock_apt_exec = MagicMock()
    mock_apt_exec.scalar_one_or_none.return_value = apt

    mock_svc = Service(
        id=1,
        clinic_id=1,
        name="General Consultation",
        description="",
        duration_minutes=30,
        buffer_minutes=15,
        price_inr=None,
        min_lead_time_hours=2,
        cancellation_window_hours=24,
        sort_order=1,
    )
    mock_svc_exec = MagicMock()
    mock_svc_exec.scalar_one_or_none.return_value = mock_svc

    session.execute.side_effect = [mock_apt_exec, mock_svc_exec]

    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CANCEL_APPOINTMENT,
        target_id="42",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        expected_target_version=1,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CANCEL_APPOINTMENT.value,
        target_id="42",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature=sig,
        expected_target_version=1,
    )

    with pytest.raises(CancellationWindowError, match="Cannot cancel within 24 hours"):
        await service.cancel_appointment(
            session=session,
            verification_manager=verification_manager,
            appointment_id=42,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            reason="Emergency",
            clinic_tz=KOLKATA_TZ,
        )


@pytest.mark.asyncio
async def test_cancel_appointment_happy_path():
    service = BookingService()
    session = make_clean_session()

    verification_manager = MagicMock(spec=CallerVerificationManager)
    verification_manager.assert_verified = AsyncMock(return_value="+919876543210")

    now = datetime.now(UTC)
    apt = Appointment(
        id=42,
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=now + timedelta(days=2, minutes=45),
        status="confirmed",
        version=1,
        created_at=now,
        updated_at=now,
    )

    mock_apt_exec = MagicMock()
    mock_apt_exec.scalar_one_or_none.return_value = apt

    mock_svc = Service(
        id=1,
        clinic_id=1,
        name="General Consultation",
        description="",
        duration_minutes=30,
        buffer_minutes=15,
        price_inr=None,
        min_lead_time_hours=2,
        cancellation_window_hours=24,
        sort_order=1,
    )
    mock_svc_exec = MagicMock()
    mock_svc_exec.scalar_one_or_none.return_value = mock_svc

    session.execute.side_effect = [mock_apt_exec, mock_svc_exec, MagicMock()]

    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CANCEL_APPOINTMENT,
        target_id="42",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        expected_target_version=1,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CANCEL_APPOINTMENT.value,
        target_id="42",
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature=sig,
        expected_target_version=1,
    )

    dto = await service.cancel_appointment(
        session=session,
        verification_manager=verification_manager,
        appointment_id=42,
        voice_session_id="sess-1",
        consent=consent,
        secret_key=VALID_SECRET,
        reason="Personal reasons",
        clinic_tz=KOLKATA_TZ,
    )

    assert dto.id == 42
    assert dto.status == "cancelled"
    assert apt.status == "cancelled"
    assert apt.version == 2
    assert session.flush.called


@pytest.mark.asyncio
async def test_confirm_booking_already_used_rejected():
    service = BookingService()
    session = make_clean_session()

    now = datetime.now(UTC)
    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        blocked_until_at=now + timedelta(days=2, minutes=45),
        state="rejected",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="k1",
        created_at=now,
    )

    mock_prop_exec = MagicMock()
    mock_prop_exec.scalar_one_or_none.return_value = proposal

    session.execute.side_effect = [mock_prop_exec]

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_BOOKING.value,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="replay-nonce",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature="dummy",
    )

    with pytest.raises(ProposalAlreadyUsedError, match="already rejected"):
        await service.confirm_booking(
            session=session,
            proposal_id=proposal.id,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            clinic_tz=KOLKATA_TZ,
        )


@pytest.mark.asyncio
async def test_confirm_reschedule_stale_version_rejected():
    service = BookingService()
    session = make_clean_session()

    verification_manager = MagicMock(spec=CallerVerificationManager)
    verification_manager.assert_verified = AsyncMock(return_value="+919876543210")

    now = datetime.now(UTC)
    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        proposal_type="reschedule",
        appointment_id=42,
        expected_appointment_version=1,  # Proposal expected version 1
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=3),
        end_at=now + timedelta(days=3, minutes=30),
        blocked_until_at=now + timedelta(days=3, minutes=45),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="k1",
        created_at=now,
    )

    apt = Appointment(
        id=42,
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Rahul",
        caller_phone="+919876543210",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=now + timedelta(days=2, minutes=45),
        status="confirmed",
        version=2,  # Current version is 2! (concurrently modified)
        created_at=now,
        updated_at=now,
    )

    mock_prop_exec = MagicMock()
    mock_prop_exec.scalar_one_or_none.return_value = proposal

    mock_apt_exec = MagicMock()
    mock_apt_exec.scalar_one_or_none.return_value = apt

    session.execute.side_effect = [mock_prop_exec, mock_apt_exec]

    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_RESCHEDULE,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        expected_target_version=1,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_RESCHEDULE.value,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature=sig,
        expected_target_version=1,
    )

    with pytest.raises(
        StaleAppointmentVersionError, match="modified after this reschedule proposal"
    ):
        await service.confirm_reschedule(
            session=session,
            verification_manager=verification_manager,
            proposal_id=proposal.id,
            voice_session_id="sess-1",
            consent=consent,
            secret_key=VALID_SECRET,
            clinic_tz=KOLKATA_TZ,
        )


@pytest.mark.asyncio
async def test_confirm_reschedule_happy_path(monkeypatch):
    monkeypatch.setattr("app.services.booking.lock_schedule_configuration", AsyncMock())
    validate = AsyncMock()
    monkeypatch.setattr("app.services.booking.validate_schedule_slot", validate)
    service = BookingService()
    session = make_clean_session()

    verification_manager = MagicMock(spec=CallerVerificationManager)
    verification_manager.assert_verified = AsyncMock(return_value="+919876543210")
    verification_manager.invalidate_verification = AsyncMock()

    now = datetime.now(UTC)
    proposal = BookingProposal(
        id=uuid4(),
        clinic_id=1,
        proposal_type="reschedule",
        appointment_id=42,
        expected_appointment_version=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        start_at=now + timedelta(days=3),
        end_at=now + timedelta(days=3, minutes=30),
        blocked_until_at=now + timedelta(days=3, minutes=45),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="k1",
        created_at=now,
    )

    apt = Appointment(
        id=42,
        clinic_id=1,
        service_id=1,
        service_name_snapshot="General Consultation",
        caller_name="Rahul",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        start_at=now + timedelta(days=2),
        end_at=now + timedelta(days=2, minutes=30),
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=now + timedelta(days=2, minutes=45),
        status="confirmed",
        version=1,
        created_at=now,
        updated_at=now,
    )

    mock_prop_exec = MagicMock()
    mock_prop_exec.scalar_one_or_none.return_value = proposal

    mock_apt_exec = MagicMock()
    mock_apt_exec.scalar_one_or_none.return_value = apt

    mock_conflict_exec = MagicMock()
    mock_conflict_exec.scalar_one_or_none.return_value = None  # No conflict for new slot

    # 1. prop lookup, 2. apt lookup, 3. advisory lock day 2, 4. advisory lock day 3, 5. conflict check
    session.execute.side_effect = [
        mock_prop_exec,
        mock_apt_exec,
        MagicMock(),
        MagicMock(),
        mock_conflict_exec,
    ]

    sig = compute_consent_signature(
        secret_key=VALID_SECRET,
        session_id="sess-1",
        action_type=ActionType.CONFIRM_RESCHEDULE,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        expected_target_version=1,
    )

    consent = ConsentAuthorization(
        session_id="sess-1",
        action_type=ActionType.CONFIRM_RESCHEDULE.value,
        target_id=str(proposal.id),
        authorized_turn_id=2,
        challenge_id="ch-1",
        nonce="nonce-1",
        created_at=now,
        expires_at=now + timedelta(seconds=300),
        ttl_seconds=300,
        signature=sig,
        expected_target_version=1,
    )

    dto = await service.confirm_reschedule(
        session=session,
        verification_manager=verification_manager,
        proposal_id=proposal.id,
        voice_session_id="sess-1",
        consent=consent,
        secret_key=VALID_SECRET,
        clinic_tz=KOLKATA_TZ,
    )

    assert dto.id == 42
    assert dto.version == 2
    assert dto.start_at == proposal.start_at
    assert proposal.state == "confirmed"
    assert session.flush.called
    validate.assert_awaited_once()
