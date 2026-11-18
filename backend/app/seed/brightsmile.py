"""Deterministic, atomic seed routine for BrightSmile Dental Clinic.

Why an explicit, transactional seed module instead of manual database inserts?
In a MERN stack application, you might run a script like `node seed.js` to insert fixtures.
In Voxora:
1. All canonical clinic facts from `PRD.md` §5.1–§5.4 are defined here as reproducible constants.
2. The entire operation is wrapped in an atomic database transaction (`session.begin()`).
   If any insert fails, all changes roll back immediately so the database is never left
   in a corrupted, half-seeded state.
3. It detects incomplete or corrupted seed states (e.g., a profile row exists without
   its corresponding hours or services) and raises an explicit error rather than silently skipping.
4. It can be run directly from the command line:
   ``python -m app.seed.brightsmile``
   with guaranteed engine disposal and proper exit codes.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models.clinic import (
    FAQ,
    BusinessClosure,
    BusinessHours,
    BusinessProfile,
    ClinicPolicy,
    Service,
)

logger = logging.getLogger("voxora.seed.brightsmile")


class IncompleteSeedError(RuntimeError):
    """Raised when a clinic profile exists but required child entities are missing."""


async def _execute_seed_in_transaction(session: AsyncSession) -> bool:
    """Core seed logic executed within an established transaction."""
    # Check if profile exists
    stmt = select(BusinessProfile).where(BusinessProfile.slug == "brightsmile")
    res = await session.execute(stmt)
    profile = res.scalar_one_or_none()

    if profile is not None:
        # Detect incomplete seed across all child tables
        hours_count = (
            await session.execute(
                select(func.count(BusinessHours.id)).where(BusinessHours.clinic_id == profile.id)
            )
        ).scalar_one()

        services_count = (
            await session.execute(
                select(func.count(Service.id)).where(Service.clinic_id == profile.id)
            )
        ).scalar_one()

        closures_count = (
            await session.execute(
                select(func.count(BusinessClosure.id)).where(
                    BusinessClosure.clinic_id == profile.id
                )
            )
        ).scalar_one()

        policies_count = (
            await session.execute(
                select(func.count(ClinicPolicy.id)).where(ClinicPolicy.clinic_id == profile.id)
            )
        ).scalar_one()

        faqs_count = (
            await session.execute(select(func.count(FAQ.id)).where(FAQ.clinic_id == profile.id))
        ).scalar_one()

        if (
            hours_count < 7
            or services_count < 4
            or closures_count < 3
            or policies_count < 2
            or faqs_count < 3
        ):
            raise IncompleteSeedError(
                f"Incomplete seed detected for 'brightsmile' (profile_id={profile.id}): "
                f"hours={hours_count}/7, services={services_count}/4, "
                f"closures={closures_count}/3, policies={policies_count}/2, "
                f"faqs={faqs_count}/3. Manual cleanup or migration required before re-seeding."
            )

        logger.info("Clinic 'brightsmile' is already fully seeded. Skipping.")
        return False

    # 1. Insert Canonical BusinessProfile (PRD §5.1)
    # Note: phone is explicitly None as a deliberate safety deviation from placeholders.
    profile = BusinessProfile(
        slug="brightsmile",
        name="BrightSmile Dental Clinic",
        description=(
            "A neighborhood dental clinic offering routine dental consultations "
            "and cosmetic/basic dental services."
        ),
        address_line1="24 Green Park Main Market",
        address_line2=None,
        city="New Delhi",
        state="Delhi",
        postal_code="110016",
        country="India",
        phone=None,
        email="contact@brightsmiledental.example",
        website="https://brightsmile.example",
        timezone="Asia/Kolkata",
    )
    session.add(profile)
    await session.flush()  # Populates profile.id dynamically

    # 2. Insert BusinessHours (PRD §5.2: Mon-Sat 09:00-18:00, break 13:00-14:00; Sun closed)
    hours_to_add: list[BusinessHours] = []
    for day in range(7):
        if day == 6:  # Sunday
            hours_to_add.append(
                BusinessHours(
                    clinic_id=profile.id,
                    day_of_week=day,
                    is_closed=True,
                    open_time=None,
                    close_time=None,
                    break_start=None,
                    break_end=None,
                )
            )
        else:  # Monday through Saturday (0-5)
            hours_to_add.append(
                BusinessHours(
                    clinic_id=profile.id,
                    day_of_week=day,
                    is_closed=False,
                    open_time=time(9, 0),
                    close_time=time(18, 0),
                    break_start=time(13, 0),
                    break_end=time(14, 0),
                )
            )
    session.add_all(hours_to_add)

    # 3. Insert BusinessClosures (Representative National Holidays)
    closures_to_add = [
        BusinessClosure(
            clinic_id=profile.id,
            date=date(2026, 1, 26),
            reason="Republic Day",
            all_day=True,
        ),
        BusinessClosure(
            clinic_id=profile.id,
            date=date(2026, 8, 15),
            reason="Independence Day",
            all_day=True,
        ),
        BusinessClosure(
            clinic_id=profile.id,
            date=date(2026, 10, 2),
            reason="Gandhi Jayanti",
            all_day=True,
        ),
    ]
    session.add_all(closures_to_add)

    # 4. Insert Canonical Services (PRD §5.3)
    services_to_add = [
        Service(
            clinic_id=profile.id,
            name="Dental consultation",
            description="Initial consultation with a licensed dentist to evaluate symptoms.",
            duration_minutes=30,
            buffer_minutes=10,
            price_inr=Decimal("500.00"),
            min_lead_time_hours=2,
            cancellation_window_hours=24,
            is_active=True,
            sort_order=1,
        ),
        Service(
            clinic_id=profile.id,
            name="Dental checkup",
            description="Routine oral exam, dental screening, and preventive care review.",
            duration_minutes=30,
            buffer_minutes=10,
            price_inr=Decimal("500.00"),
            min_lead_time_hours=2,
            cancellation_window_hours=24,
            is_active=True,
            sort_order=2,
        ),
        Service(
            clinic_id=profile.id,
            name="Teeth cleaning",
            description="Professional plaque, tartar removal, and teeth polishing.",
            duration_minutes=45,
            buffer_minutes=10,
            price_inr=Decimal("1500.00"),
            min_lead_time_hours=2,
            cancellation_window_hours=24,
            is_active=True,
            sort_order=3,
        ),
        Service(
            clinic_id=profile.id,
            name="Teeth whitening",
            description="Cosmetic in-clinic teeth whitening and enamel brightening procedure.",
            duration_minutes=60,
            buffer_minutes=15,
            price_inr=Decimal("4000.00"),
            min_lead_time_hours=2,
            cancellation_window_hours=24,
            is_active=True,
            sort_order=4,
        ),
    ]
    session.add_all(services_to_add)

    # 5. Insert Canonical Policies (Consistent with service cancellation window)
    policies_to_add = [
        ClinicPolicy(
            clinic_id=profile.id,
            policy_type="cancellation",
            title="Cancellation Policy",
            content=(
                "Appointments may be rescheduled or cancelled up to 24 hours in advance "
                "at no charge. For cancellations with less than 24 hours notice, please "
                "speak directly with clinic staff."
            ),
            sort_order=1,
        ),
        ClinicPolicy(
            clinic_id=profile.id,
            policy_type="general",
            title="Clinic Boundaries and Scope",
            content=(
                "BrightSmile Dental Clinic provides routine consultations and basic "
                "dental procedures. We do not provide emergency surgical trauma care "
                "or inpatient treatment."
            ),
            sort_order=2,
        ),
    ]
    session.add_all(policies_to_add)

    # 6. Insert Canonical FAQs (Strictly grounded in PRD §5.4; no unverified facts)
    faqs_to_add = [
        FAQ(
            clinic_id=profile.id,
            topic="location",
            question="Where is the clinic located?",
            answer=(
                "BrightSmile Dental Clinic is located at 24 Green Park Main Market, "
                "New Delhi, Delhi 110016."
            ),
            is_active=True,
            sort_order=1,
        ),
        FAQ(
            clinic_id=profile.id,
            topic="hours",
            question="What are your operating hours?",
            answer=(
                "We are open Monday through Saturday from 9:00 AM to 6:00 PM, "
                "with a lunch break between 1:00 PM and 2:00 PM. We are closed on Sundays."
            ),
            is_active=True,
            sort_order=2,
        ),
        FAQ(
            clinic_id=profile.id,
            topic="appointments",
            question="Do I need an appointment before visiting?",
            answer=(
                "Appointments are strongly recommended to guarantee a slot. "
                "We do not promise walk-in availability."
            ),
            is_active=True,
            sort_order=3,
        ),
    ]
    session.add_all(faqs_to_add)

    # Note: OwnerUser is NOT seeded here. Phase 7 will handle secure owner provisioning.
    logger.info("Successfully seeded canonical data for BrightSmile Dental Clinic.")
    return True


async def seed_brightsmile(session: AsyncSession) -> bool:
    """Atomically seed BrightSmile Dental Clinic configuration.

    Transaction & Session Assumptions:
    - Entire operation runs inside a single atomic transaction.
    - If the session already has an active transaction (session.in_transaction()),
      participates directly in that transaction.
    - If no transaction is active, encapsulates the entire existence check,
      completeness check, and all inserts inside `async with session.begin():`.
    - If the clinic is already fully seeded, commits nothing and returns `False`.
    - If an incomplete seed is detected, raises `IncompleteSeedError`.
    - If any error occurs, rolls back completely, leaving 0 partial rows.

    Returns:
        bool: True if seeded successfully, False if already fully seeded.
    """
    in_tx = session.in_transaction()
    if asyncio.iscoroutine(in_tx):
        in_tx = await in_tx
    if in_tx:
        return await _execute_seed_in_transaction(session)
    async with session.begin():
        return await _execute_seed_in_transaction(session)


async def main() -> None:
    """CLI entrypoint for running the seed routine with complete lifecycle management."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = get_settings()

    engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with session_factory() as session:
            seeded = await seed_brightsmile(session)
            if seeded:
                print("Seed complete: BrightSmile Dental Clinic configured successfully.")
            else:
                print("Seed skipped: BrightSmile Dental Clinic already exists.")
    except Exception as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
