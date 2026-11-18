"""Unit tests for FastAPI booking and availability endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.models.clinic import BusinessProfile, Service
from app.db.session import get_db
from app.main import app
from app.schemas.dto import AppointmentDTO, ProposalDTO, SlotDTO
from app.services.booking_errors import (
    InvalidConsentError,
    ProposalExpiredError,
    SlotUnavailableError,
)

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")
VALID_SECRET = "voxora_test_secret_key_32_characters_long_min"


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=False)
    session.begin = MagicMock(return_value=AsyncMock())
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def test_client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_db] = lambda: mock_session
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client


# =============================================================================
# Availability Route Tests
# =============================================================================


@pytest.mark.asyncio
async def test_get_availability_invalid_date_order(test_client: AsyncClient):
    resp = await test_client.get(
        "/api/availability",
        params={
            "clinic_id": 1,
            "service_id": 1,
            "from_date": "2026-11-26",
            "to_date": "2026-11-25",
        },
    )
    assert resp.status_code == 422
    assert "to_date must be greater than or equal to from_date" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_availability_range_too_wide(test_client: AsyncClient):
    resp = await test_client.get(
        "/api/availability",
        params={
            "clinic_id": 1,
            "service_id": 1,
            "from_date": "2026-11-01",
            "to_date": "2026-12-05",  # > 30 days
        },
    )
    assert resp.status_code == 422
    assert "cannot exceed 30 days" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_availability_happy_path(test_client: AsyncClient, mock_session: AsyncMock):
    profile = BusinessProfile(
        id=1,
        name="BrightSmile Dental Clinic",
        slug="brightsmile",
        phone="+919876543210",
        timezone="Asia/Kolkata",
    )
    service = Service(
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
        is_active=True,
    )

    with (
        patch("app.api.routes_appointments.get_profile_by_id", AsyncMock(return_value=profile)),
        patch("app.api.routes_appointments.get_service_by_id", AsyncMock(return_value=service)),
        patch("app.api.routes_appointments.get_hours_by_clinic_id", AsyncMock(return_value=[])),
        patch(
            "app.api.routes_appointments.get_upcoming_closures_by_clinic_id",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.routes_appointments.compute_available_slots",
            return_value=[
                SlotDTO(
                    start_at=datetime(2026, 11, 25, 10, 0, tzinfo=UTC),
                    end_at=datetime(2026, 11, 25, 10, 30, tzinfo=UTC),
                    duration_minutes=30,
                    buffer_minutes=15,
                    blocked_until_at=datetime(2026, 11, 25, 10, 45, tzinfo=UTC),
                )
            ],
        ),
    ):
        mock_apt_exec = MagicMock()
        mock_apt_exec.scalars.return_value.all.return_value = []
        mock_prop_exec = MagicMock()
        mock_prop_exec.scalars.return_value.all.return_value = []
        mock_session.execute.side_effect = [mock_apt_exec, mock_prop_exec]

        resp = await test_client.get(
            "/api/availability",
            params={
                "clinic_id": 1,
                "service_id": 1,
                "from_date": "2026-11-25",
                "to_date": "2026-11-25",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["clinic_id"] == 1
        assert data["service_id"] == 1
        assert len(data["slots"]) == 1
        assert data["slots"][0]["duration_minutes"] == 30
        # A pending proposal is not a slot hold; only confirmed appointments are queried.
        mock_session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_availability_splits_long_range_without_gaps(mock_session: AsyncMock):
    from datetime import date

    from app.api.routes_appointments import get_availability

    profile = SimpleNamespace(timezone="Asia/Kolkata")
    service = SimpleNamespace(id=1, name="Exam", description=None, is_active=True,
        duration_minutes=30, buffer_minutes=15, price_inr=None, min_lead_time_hours=0,
        cancellation_window_hours=24, sort_order=1)
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = result
    with (
        patch("app.api.routes_appointments.get_profile_by_id", AsyncMock(return_value=profile)),
        patch("app.api.routes_appointments.get_service_by_id", AsyncMock(return_value=service)),
        patch("app.api.routes_appointments.get_hours_by_clinic_id", AsyncMock(return_value=())),
        patch("app.api.routes_appointments.get_upcoming_closures_by_clinic_id", AsyncMock(return_value=())),
        patch("app.api.routes_appointments.compute_available_slots", return_value=[]) as compute,
    ):
        await get_availability(1, 1, date(2027, 1, 1), date(2027, 1, 31), mock_session)
    assert [(c.kwargs["start_date"], c.kwargs["end_date"]) for c in compute.call_args_list] == [
        (date(2027, 1, 1), date(2027, 1, 15)),
        (date(2027, 1, 16), date(2027, 1, 30)),
        (date(2027, 1, 31), date(2027, 1, 31)),
    ]


# =============================================================================
# Propose Booking Route Tests
# =============================================================================


@pytest.mark.asyncio
async def test_propose_booking_route_happy_path(test_client: AsyncClient):
    prop_id = uuid4()
    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    mock_dto = ProposalDTO(
        id=prop_id,
        clinic_id=1,
        proposal_type="new_booking",
        appointment_id=None,
        confirmed_appointment_id=None,
        expected_appointment_version=None,
        service_id=1,
        service_name="General Consultation",
        session_id="sess-1",
        created_turn_id=1,
        duration_minutes=30,
        buffer_minutes=15,
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        start_at=now,
        end_at=now + timedelta(minutes=30),
        blocked_until_at=now + timedelta(minutes=45),
        state="pending",
        expires_at=now + timedelta(minutes=15),
        idempotency_key="idemp-key",
        created_at=now,
        consumed_at=None,
    )

    with patch(
        "app.api.routes_appointments.booking_service.propose_booking",
        AsyncMock(return_value=mock_dto),
    ):
        resp = await test_client.post(
            "/api/proposals",
            json={
                "clinic_id": 1,
                "service_id": 1,
                "session_id": "sess-1",
                "created_turn_id": 1,
                "caller_name": "Rahul Sharma",
                "caller_phone": "+919876543210",
                "caller_email": "rahul@example.com",
                "start_at": now.isoformat(),
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == str(prop_id)
        assert data["service_name"] == "General Consultation"
        assert data["state"] == "pending"


@pytest.mark.asyncio
async def test_propose_booking_route_slot_unavailable(test_client: AsyncClient):
    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    with patch(
        "app.api.routes_appointments.booking_service.propose_booking",
        AsyncMock(side_effect=SlotUnavailableError("Requested slot is unavailable.")),
    ):
        resp = await test_client.post(
            "/api/proposals",
            json={
                "clinic_id": 1,
                "service_id": 1,
                "session_id": "sess-1",
                "created_turn_id": 1,
                "caller_name": "Rahul Sharma",
                "caller_phone": "+919876543210",
                "start_at": now.isoformat(),
            },
        )
        assert resp.status_code == 409
        assert "Requested slot is unavailable" in resp.json()["detail"]


# =============================================================================
# Confirm Booking Route Tests
# =============================================================================


@pytest.mark.asyncio
async def test_confirm_booking_route_happy_path(test_client: AsyncClient, mock_session: AsyncMock):
    prop_id = uuid4()
    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    apt_dto = AppointmentDTO(
        id=42,
        clinic_id=1,
        service_id=1,
        service_name="General Consultation",
        caller_name="Rahul Sharma",
        caller_phone="+919876543210",
        caller_email="rahul@example.com",
        start_at=now,
        end_at=now + timedelta(minutes=30),
        duration_minutes=30,
        buffer_minutes=15,
        blocked_until_at=now + timedelta(minutes=45),
        status="confirmed",
        version=1,
        cancellation_reason=None,
        created_at=now,
        updated_at=now,
    )

    with (
        patch("app.api.routes_appointments.get_profile_by_id", AsyncMock(return_value=None)),
        patch(
            "app.api.routes_appointments.booking_service.confirm_booking",
            AsyncMock(return_value=apt_dto),
        ),
    ):
        resp = await test_client.post(
            f"/api/proposals/{prop_id}/confirm",
            json={
                "voice_session_id": "sess-1",
                "consent": {
                    "session_id": "sess-1",
                    "action_type": "confirm_booking",
                    "target_id": str(prop_id),
                    "authorized_turn_id": 2,
                    "challenge_id": "ch-1",
                    "nonce": "n-1",
                    "created_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=300)).isoformat(),
                    "ttl_seconds": 300,
                    "signature": "valid-sig",
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 42
        assert data["status"] == "confirmed"
        assert data["version"] == 1


@pytest.mark.asyncio
async def test_confirm_booking_route_expired_proposal(test_client: AsyncClient):
    prop_id = uuid4()
    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)

    with (
        patch("app.api.routes_appointments.get_profile_by_id", AsyncMock(return_value=None)),
        patch(
            "app.api.routes_appointments.booking_service.confirm_booking",
            AsyncMock(side_effect=ProposalExpiredError("Proposal has expired.")),
        ),
    ):
        resp = await test_client.post(
            f"/api/proposals/{prop_id}/confirm",
            json={
                "voice_session_id": "sess-1",
                "consent": {
                    "session_id": "sess-1",
                    "action_type": "confirm_booking",
                    "target_id": str(prop_id),
                    "authorized_turn_id": 2,
                    "challenge_id": "ch-1",
                    "nonce": "n-1",
                    "created_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=300)).isoformat(),
                    "ttl_seconds": 300,
                    "signature": "valid-sig",
                },
            },
        )
        assert resp.status_code == 410
        assert "has expired" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_confirm_booking_route_invalid_consent(test_client: AsyncClient):
    prop_id = uuid4()
    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)

    with (
        patch("app.api.routes_appointments.get_profile_by_id", AsyncMock(return_value=None)),
        patch(
            "app.api.routes_appointments.booking_service.confirm_booking",
            AsyncMock(side_effect=InvalidConsentError("Signature verification failed.")),
        ),
    ):
        resp = await test_client.post(
            f"/api/proposals/{prop_id}/confirm",
            json={
                "voice_session_id": "sess-1",
                "consent": {
                    "session_id": "sess-1",
                    "action_type": "confirm_booking",
                    "target_id": str(prop_id),
                    "authorized_turn_id": 2,
                    "challenge_id": "ch-1",
                    "nonce": "n-1",
                    "created_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=300)).isoformat(),
                    "ttl_seconds": 300,
                    "signature": "bad-sig",
                },
            },
        )
        assert resp.status_code == 400
        assert "Signature verification failed" in resp.json()["detail"]
