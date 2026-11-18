"""Unit tests for FastAPI read-only clinic endpoints.

These tests use FastAPI's dependency override system to test endpoints offline
with mocked repository returns, verifying response formats, HTTP status codes,
and 404 handling.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_db
from app.main import create_app
from app.schemas.dto import (
    FAQDTO,
    BusinessClosureDTO,
    BusinessHoursDTO,
    ClinicPolicyDTO,
    ClinicProfileDTO,
    ServiceDTO,
)


@pytest.fixture
def test_app():
    return create_app()


@pytest.fixture
def mock_profile():
    return ClinicProfileDTO(
        id=1,
        slug="brightsmile",
        name="BrightSmile Dental Clinic",
        description="Demo dental clinic",
        address_line1="24 Green Park Main Market",
        address_line2=None,
        city="New Delhi",
        state="Delhi",
        postal_code="110016",
        country="India",
        phone=None,
        email="contact@brightsmile.example",
        website="https://brightsmile.example",
        timezone="Asia/Kolkata",
    )


@pytest.mark.asyncio
async def test_get_clinic_profile_200(test_app, mock_profile) -> None:
    async def override_get_db():
        yield AsyncMock()

    test_app.dependency_overrides[get_db] = override_get_db

    with patch("app.api.routes_business.get_profile_by_slug", AsyncMock(return_value=mock_profile)):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/clinic?slug=brightsmile")

    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "brightsmile"
    assert data["name"] == "BrightSmile Dental Clinic"
    assert data["city"] == "New Delhi"
    assert data["timezone"] == "Asia/Kolkata"
    assert data["phone"] is None


@pytest.mark.asyncio
async def test_get_clinic_profile_404(test_app) -> None:
    async def override_get_db():
        yield AsyncMock()

    test_app.dependency_overrides[get_db] = override_get_db

    with patch("app.api.routes_business.get_profile_by_slug", AsyncMock(return_value=None)):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/clinic?slug=unknown")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_clinic_hours_200(test_app, mock_profile) -> None:
    async def override_get_db():
        yield AsyncMock()

    test_app.dependency_overrides[get_db] = override_get_db

    mock_hours = (
        BusinessHoursDTO(
            day_of_week=0,
            is_closed=False,
            open_time=time(9, 0),
            close_time=time(18, 0),
            break_start=time(13, 0),
            break_end=time(14, 0),
        ),
    )

    with (
        patch("app.api.routes_business.get_profile_by_slug", AsyncMock(return_value=mock_profile)),
        patch("app.api.routes_business.get_hours_by_clinic_id", AsyncMock(return_value=mock_hours)),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/clinic/hours?slug=brightsmile")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["day_of_week"] == 0
    assert data[0]["open_time"] == "09:00:00"


@pytest.mark.asyncio
async def test_get_clinic_services_200(test_app, mock_profile) -> None:
    async def override_get_db():
        yield AsyncMock()

    test_app.dependency_overrides[get_db] = override_get_db

    mock_services = (
        ServiceDTO(
            id=1,
            name="Dental consultation",
            description="Initial exam",
            duration_minutes=30,
            buffer_minutes=10,
            price_inr=Decimal("500.00"),
            min_lead_time_hours=2,
            cancellation_window_hours=24,
            sort_order=1,
        ),
    )

    p_slug = patch(
        "app.api.routes_business.get_profile_by_slug",
        AsyncMock(return_value=mock_profile),
    )
    p_services = patch(
        "app.api.routes_business.get_active_services_by_clinic_id",
        AsyncMock(return_value=mock_services),
    )
    with p_slug, p_services:
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/clinic/services?slug=brightsmile")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Dental consultation"
    assert float(data[0]["price_inr"]) == 500.0


@pytest.mark.asyncio
async def test_get_clinic_closures_policies_faqs_200(test_app, mock_profile) -> None:
    async def override_get_db():
        yield AsyncMock()

    test_app.dependency_overrides[get_db] = override_get_db

    mock_closures = (
        BusinessClosureDTO(date=date(2026, 1, 26), reason="Republic Day", all_day=True),
    )
    mock_policies = (
        ClinicPolicyDTO(policy_type="cancellation", title="Cancel", content="24h", sort_order=1),
    )
    mock_faqs = (FAQDTO(id=1, topic="location", question="Where?", answer="Delhi", sort_order=1),)

    p_slug = patch(
        "app.api.routes_business.get_profile_by_slug",
        AsyncMock(return_value=mock_profile),
    )
    p_closures = patch(
        "app.api.routes_business.get_upcoming_closures_by_clinic_id",
        AsyncMock(return_value=mock_closures),
    )
    p_policies = patch(
        "app.api.routes_business.get_policies_by_clinic_id",
        AsyncMock(return_value=mock_policies),
    )
    p_faqs = patch(
        "app.api.routes_business.get_active_faqs_by_clinic_id",
        AsyncMock(return_value=mock_faqs),
    )

    with p_slug, p_closures, p_policies, p_faqs:
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            r_closures = await client.get("/api/clinic/closures?slug=brightsmile")
            r_policies = await client.get("/api/clinic/policies?slug=brightsmile")
            r_faqs = await client.get("/api/clinic/faqs?slug=brightsmile")

    assert r_closures.status_code == 200
    assert r_policies.status_code == 200
    assert r_faqs.status_code == 200
    assert r_closures.json()[0]["reason"] == "Republic Day"
    assert r_policies.json()[0]["policy_type"] == "cancellation"
    assert r_faqs.json()[0]["question"] == "Where?"
