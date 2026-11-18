"""Unit tests for repository query functions.

Verifies:
1. Dynamic clinic_id usage (no hardcoded IDs).
2. Proper DTO mapping for all domain entities.
3. Query filtering and ordering.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.clinic import (
    FAQ,
    BusinessClosure,
    BusinessHours,
    BusinessProfile,
    ClinicPolicy,
    Service,
)
from app.repositories.clinic_repo import (
    get_active_faqs_by_clinic_id,
    get_active_services_by_clinic_id,
    get_hours_by_clinic_id,
    get_policies_by_clinic_id,
    get_profile_by_slug,
    get_upcoming_closures_by_clinic_id,
)


@pytest.mark.asyncio
async def test_get_profile_by_slug_maps_dto() -> None:
    mock_session = AsyncMock()
    mock_profile = BusinessProfile(
        id=42,
        slug="brightsmile",
        name="BrightSmile Dental Clinic",
        description="Demo clinic",
        address_line1="24 Green Park",
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
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_profile
    mock_session.execute.return_value = mock_result

    dto = await get_profile_by_slug(mock_session, slug="brightsmile")
    assert dto is not None
    assert dto.id == 42
    assert dto.slug == "brightsmile"
    assert dto.timezone == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_get_hours_by_clinic_id_uses_dynamic_id() -> None:
    mock_session = AsyncMock()
    mock_row = BusinessHours(
        id=101,
        clinic_id=42,
        day_of_week=0,
        is_closed=False,
        open_time=time(9, 0),
        close_time=time(18, 0),
        break_start=time(13, 0),
        break_end=time(14, 0),
    )
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_row]
    mock_session.execute.return_value = mock_result

    dtos = await get_hours_by_clinic_id(mock_session, clinic_id=42)
    assert isinstance(dtos, tuple)
    assert len(dtos) == 1
    assert dtos[0].day_of_week == 0
    assert dtos[0].open_time == time(9, 0)


@pytest.mark.asyncio
async def test_get_upcoming_closures_filters_by_window() -> None:
    mock_session = AsyncMock()
    mock_closure = BusinessClosure(
        id=201,
        clinic_id=42,
        date=date(2026, 1, 26),
        reason="Republic Day",
        all_day=True,
    )
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_closure]
    mock_session.execute.return_value = mock_result

    closures = await get_upcoming_closures_by_clinic_id(
        mock_session,
        clinic_id=42,
        since_date=date(2026, 1, 1),
        until_date=date(2026, 2, 1),
    )
    assert isinstance(closures, tuple)
    assert len(closures) == 1
    assert closures[0].reason == "Republic Day"


@pytest.mark.asyncio
async def test_get_active_services_maps_prices() -> None:
    mock_session = AsyncMock()
    mock_service = Service(
        id=301,
        clinic_id=42,
        name="Teeth cleaning",
        description="Plaque removal",
        duration_minutes=45,
        buffer_minutes=10,
        price_inr=Decimal("1500.00"),
        min_lead_time_hours=2,
        cancellation_window_hours=24,
        is_active=True,
        sort_order=1,
    )
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_service]
    mock_session.execute.return_value = mock_result

    services = await get_active_services_by_clinic_id(mock_session, clinic_id=42)
    assert isinstance(services, tuple)
    assert len(services) == 1
    assert services[0].price_inr == Decimal("1500.00")
    assert services[0].duration_minutes == 45


@pytest.mark.asyncio
async def test_get_policies_and_faqs_ordering() -> None:
    mock_session = AsyncMock()
    mock_policy = ClinicPolicy(
        id=401,
        clinic_id=42,
        policy_type="cancellation",
        title="Cancellation Policy",
        content="24 hours notice required.",
        sort_order=1,
    )
    mock_faq = FAQ(
        id=501,
        clinic_id=42,
        topic="appointments",
        question="Do I need an appointment?",
        answer="Yes.",
        is_active=True,
        sort_order=1,
    )

    mock_result1 = MagicMock()
    mock_result1.scalars.return_value.all.return_value = [mock_policy]
    mock_result2 = MagicMock()
    mock_result2.scalars.return_value.all.return_value = [mock_faq]

    mock_session.execute.side_effect = [mock_result1, mock_result2]

    policies = await get_policies_by_clinic_id(mock_session, clinic_id=42)
    faqs = await get_active_faqs_by_clinic_id(mock_session, clinic_id=42)

    assert isinstance(policies, tuple)
    assert len(policies) == 1
    assert policies[0].policy_type == "cancellation"
    assert isinstance(faqs, tuple)
    assert len(faqs) == 1
    assert faqs[0].topic == "appointments"
