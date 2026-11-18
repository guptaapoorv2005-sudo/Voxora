"""Repository functions for querying clinic configuration from PostgreSQL.

In a MERN application, this would be your Data Access Layer (e.g. Mongoose queries).
In Voxora, these functions execute asynchronous SQLAlchemy queries against Neon PostgreSQL
and convert the resulting database rows into immutable frozen DTOs (`app.schemas.dto`).

Crucial Architectural Rule:
All child query functions accept an explicit `clinic_id: int` resolved dynamically from
`get_profile_by_slug()`. No query hardcodes clinic IDs.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.clinic import (
    FAQ,
    BusinessClosure,
    BusinessHours,
    BusinessProfile,
    ClinicPolicy,
    Service,
)
from app.schemas.dto import (
    FAQDTO,
    BusinessClosureDTO,
    BusinessHoursDTO,
    ClinicPolicyDTO,
    ClinicProfileDTO,
    ServiceDTO,
)


async def get_profile_by_slug(
    session: AsyncSession,
    slug: str = "brightsmile",
) -> ClinicProfileDTO | None:
    """Fetch the canonical clinic profile by unique slug.

    Args:
        session: Active SQLAlchemy async session.
        slug: Unique clinic identifier (e.g. 'brightsmile').

    Returns:
        `ClinicProfileDTO` if found, else None.
    """
    stmt = select(BusinessProfile).where(BusinessProfile.slug == slug.strip())
    result = await session.execute(stmt)
    profile = result.scalar_one_or_none()
    if profile is None:
        return None

    return ClinicProfileDTO(
        id=profile.id,
        slug=profile.slug,
        name=profile.name,
        description=profile.description,
        address_line1=profile.address_line1,
        address_line2=profile.address_line2,
        city=profile.city,
        state=profile.state,
        postal_code=profile.postal_code,
        country=profile.country,
        phone=profile.phone,
        email=profile.email,
        website=profile.website,
        timezone=profile.timezone,
    )


async def get_hours_by_clinic_id(
    session: AsyncSession,
    clinic_id: int,
) -> tuple[BusinessHoursDTO, ...]:
    """Fetch weekly operating schedule for a clinic, ordered 0 to 6 (Mon..Sun).

    Args:
        session: Active SQLAlchemy async session.
        clinic_id: Database ID of the clinic.

    Returns:
        Tuple of 7 `BusinessHoursDTO` instances.
    """
    stmt = (
        select(BusinessHours)
        .where(BusinessHours.clinic_id == clinic_id)
        .order_by(BusinessHours.day_of_week.asc())
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    return tuple(
        BusinessHoursDTO(
            day_of_week=r.day_of_week,
            is_closed=r.is_closed,
            open_time=r.open_time,
            close_time=r.close_time,
            break_start=r.break_start,
            break_end=r.break_end,
        )
        for r in records
    )


async def get_upcoming_closures_by_clinic_id(
    session: AsyncSession,
    clinic_id: int,
    since_date: date,
    until_date: date,
) -> tuple[BusinessClosureDTO, ...]:
    """Fetch all closures and public holidays within a calendar window.

    Args:
        session: Active SQLAlchemy async session.
        clinic_id: Database ID of the clinic.
        since_date: Start date (inclusive) in clinic local time.
        until_date: End date (inclusive) in clinic local time.

    Returns:
        Tuple of `BusinessClosureDTO` instances ordered chronologically.
    """
    stmt = (
        select(BusinessClosure)
        .where(
            BusinessClosure.clinic_id == clinic_id,
            BusinessClosure.date >= since_date,
            BusinessClosure.date <= until_date,
        )
        .order_by(BusinessClosure.date.asc())
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    return tuple(
        BusinessClosureDTO(
            date=r.date,
            reason=r.reason,
            all_day=r.all_day,
        )
        for r in records
    )


async def get_active_services_by_clinic_id(
    session: AsyncSession,
    clinic_id: int,
) -> tuple[ServiceDTO, ...]:
    """Fetch all active dental services offered by the clinic.

    Args:
        session: Active SQLAlchemy async session.
        clinic_id: Database ID of the clinic.

    Returns:
        Tuple of active `ServiceDTO` instances ordered by sort_order and name.
    """
    stmt = (
        select(Service)
        .where(
            Service.clinic_id == clinic_id,
            Service.is_active.is_(True),
        )
        .order_by(Service.sort_order.asc(), Service.name.asc())
        .execution_options(populate_existing=True)
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    return tuple(
        ServiceDTO(
            id=r.id,
            name=r.name,
            description=r.description,
            duration_minutes=r.duration_minutes,
            buffer_minutes=r.buffer_minutes,
            price_inr=r.price_inr,
            min_lead_time_hours=r.min_lead_time_hours,
            cancellation_window_hours=r.cancellation_window_hours,
            sort_order=r.sort_order,
        )
        for r in records
    )


async def get_policies_by_clinic_id(
    session: AsyncSession,
    clinic_id: int,
) -> tuple[ClinicPolicyDTO, ...]:
    """Fetch all clinic policies (cancellation, general, etc.).

    Args:
        session: Active SQLAlchemy async session.
        clinic_id: Database ID of the clinic.

    Returns:
        Tuple of `ClinicPolicyDTO` instances ordered by sort_order.
    """
    stmt = (
        select(ClinicPolicy)
        .where(ClinicPolicy.clinic_id == clinic_id)
        .order_by(ClinicPolicy.sort_order.asc())
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    return tuple(
        ClinicPolicyDTO(
            policy_type=r.policy_type,
            title=r.title,
            content=r.content,
            sort_order=r.sort_order,
        )
        for r in records
    )


async def get_active_faqs_by_clinic_id(
    session: AsyncSession,
    clinic_id: int,
) -> tuple[FAQDTO, ...]:
    """Fetch all active FAQs grounded strictly in clinic records.

    Args:
        session: Active SQLAlchemy async session.
        clinic_id: Database ID of the clinic.

    Returns:
        Tuple of active `FAQDTO` instances ordered by sort_order and id.
    """
    stmt = (
        select(FAQ)
        .where(
            FAQ.clinic_id == clinic_id,
            FAQ.is_active.is_(True),
        )
        .order_by(FAQ.sort_order.asc(), FAQ.id.asc())
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    return tuple(
        FAQDTO(
            id=r.id,
            topic=r.topic,
            question=r.question,
            answer=r.answer,
            sort_order=r.sort_order,
        )
        for r in records
    )


async def get_profile_by_id(
    session: AsyncSession,
    clinic_id: int,
) -> ClinicProfileDTO | None:
    """Fetch the clinic profile by ID."""
    stmt = select(BusinessProfile).where(BusinessProfile.id == clinic_id)
    result = await session.execute(stmt)
    profile = result.scalar_one_or_none()
    if profile is None:
        return None

    return ClinicProfileDTO(
        id=profile.id,
        slug=profile.slug,
        name=profile.name,
        description=profile.description,
        address_line1=profile.address_line1,
        address_line2=profile.address_line2,
        city=profile.city,
        state=profile.state,
        postal_code=profile.postal_code,
        country=profile.country,
        phone=profile.phone,
        email=profile.email,
        website=profile.website,
        timezone=profile.timezone,
    )


async def get_service_by_id(
    session: AsyncSession,
    clinic_id: int,
    service_id: int,
) -> Service | None:
    """Fetch service ORM entity by clinic ID and service ID."""
    stmt = select(Service).where(
        Service.id == service_id,
        Service.clinic_id == clinic_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
