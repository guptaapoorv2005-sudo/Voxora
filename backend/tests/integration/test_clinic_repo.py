"""Integration tests for Phase 3 clinic configuration and repositories.

These tests run against a real, isolated Neon test database when `TEST_DATABASE_URL` is set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.clinic import Service
from app.repositories.clinic_repo import (
    get_active_faqs_by_clinic_id,
    get_active_services_by_clinic_id,
    get_hours_by_clinic_id,
    get_policies_by_clinic_id,
    get_profile_by_slug,
    get_upcoming_closures_by_clinic_id,
)
from app.seed.brightsmile import seed_brightsmile
from app.services.clinic_context import load_clinic_context


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_and_query_roundtrip(test_db_session: AsyncSession) -> None:
    """Verify seed and repository queries execute correctly on Neon PostgreSQL."""
    # Run seed
    seeded = await seed_brightsmile(test_db_session)
    assert seeded in (True, False)

    # 1. Profile
    profile = await get_profile_by_slug(test_db_session, slug="brightsmile")
    assert profile is not None
    assert profile.name == "BrightSmile Dental Clinic"
    assert profile.city == "New Delhi"
    assert profile.timezone == "Asia/Kolkata"
    assert profile.phone is None

    # 2. Hours
    hours = await get_hours_by_clinic_id(test_db_session, profile.id)
    assert len(hours) == 7
    # Sunday is closed
    sunday = next(h for h in hours if h.day_of_week == 6)
    assert sunday.is_closed is True
    assert sunday.open_time is None

    # 3. Closures
    closures = await get_upcoming_closures_by_clinic_id(
        test_db_session,
        profile.id,
        since_date=datetime(2026, 1, 1).date(),
        until_date=datetime(2026, 12, 31).date(),
    )
    assert len(closures) >= 3
    reasons = [c.reason for c in closures]
    assert "Republic Day" in reasons
    assert "Independence Day" in reasons

    # 4. Services
    services = await get_active_services_by_clinic_id(test_db_session, profile.id)
    assert len(services) >= 4
    service_names = [s.name for s in services]
    assert "Dental consultation" in service_names
    assert "Teeth whitening" in service_names

    # 5. Policies
    policies = await get_policies_by_clinic_id(test_db_session, profile.id)
    assert len(policies) >= 2
    types = [p.policy_type for p in policies]
    assert "cancellation" in types

    # 6. FAQs
    faqs = await get_active_faqs_by_clinic_id(test_db_session, profile.id)
    assert len(faqs) >= 3

    # 7. Complete Context Loader round-trip
    ctx = await load_clinic_context(
        test_db_session,
        slug="brightsmile",
        now=datetime.now(UTC),
    )
    assert ctx.profile.slug == "brightsmile"
    assert len(ctx.hours) == 7
    # Verify collections are immutable tuples
    assert isinstance(ctx.hours, tuple)
    assert isinstance(ctx.closures, tuple)
    assert isinstance(ctx.services, tuple)
    assert isinstance(ctx.policies, tuple)
    assert isinstance(ctx.faqs, tuple)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_idempotency_on_real_db(test_db_session: AsyncSession) -> None:
    """Verify that re-running seed on an already seeded database is a clean no-op."""
    await seed_brightsmile(test_db_session)
    # The second run must detect existing seed and skip cleanly without error
    second = await seed_brightsmile(test_db_session)
    assert second is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_transaction_boundary_with_prior_query(test_db_session: AsyncSession) -> None:
    """Regression test: query before calling seed_brightsmile does not raise error."""
    # Execute a query first, causing SQLAlchemy to autobegin a transaction on this session
    from sqlalchemy import select

    from app.db.models.clinic import BusinessProfile

    await test_db_session.execute(select(BusinessProfile).limit(1))
    assert test_db_session.in_transaction()

    # seed_brightsmile must detect in_transaction() and participate cleanly
    result = await seed_brightsmile(test_db_session)
    assert result in (True, False)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_check_constraints_enforced(test_db_session: AsyncSession) -> None:
    """Verify that database CheckConstraints reject invalid data at PostgreSQL level."""
    profile = await get_profile_by_slug(test_db_session, slug="brightsmile")
    assert profile is not None

    # Attempt to insert an invalid service with negative duration
    invalid_service = Service(
        clinic_id=profile.id,
        name="Invalid Service",
        duration_minutes=-15,  # Violates chk_service_duration_positive
        buffer_minutes=10,
        price_inr=Decimal("100.00"),
    )
    test_db_session.add(invalid_service)

    with pytest.raises(IntegrityError):
        await test_db_session.flush()

    await test_db_session.rollback()
