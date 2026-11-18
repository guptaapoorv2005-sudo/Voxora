"""Validate a proposed slot against current clinic configuration."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.clinic_repo import (
    get_active_services_by_clinic_id,
    get_hours_by_clinic_id,
    get_profile_by_id,
    get_upcoming_closures_by_clinic_id,
)
from app.services.availability import compute_available_slots
from app.services.booking_errors import SlotUnavailableError
from app.voice.context import resolve_clinic_timezone


async def lock_schedule_configuration(session: AsyncSession, clinic_id: int) -> None:
    """Serialize configuration writes with validation; key 0 is outside YYYYMMDD.

    Confirmation takes this lock only after its calendar-date locks. Configuration
    writers must never take appointment, proposal, challenge, or date locks while
    holding it, so the existing booking lock order has no reverse edge.
    """
    await session.execute(select(func.pg_advisory_xact_lock(clinic_id, 0)))


async def validate_schedule_slot(
    session: AsyncSession,
    *,
    clinic_id: int,
    service_id: int,
    start_at: datetime,
    duration_minutes: int,
    buffer_minutes: int,
    service_name: str,
    now: datetime,
) -> None:
    """Fail closed if the proposed snapshot no longer matches the current schedule.

    Caller must hold the configuration lock until its transaction completes. Existing
    appointment conflicts are checked separately under calendar-date locks.
    """
    profile = await get_profile_by_id(session, clinic_id)
    if profile is None or start_at.tzinfo is None or now.tzinfo is None:
        raise SlotUnavailableError("Requested slot is no longer available.")
    services = await get_active_services_by_clinic_id(session, clinic_id)
    service = next((item for item in services if item.id == service_id), None)
    if (
        service is None
        or service.duration_minutes != duration_minutes
        or service.buffer_minutes != buffer_minutes
        or service.name != service_name
    ):
        raise SlotUnavailableError("Service details have changed; request a new slot.")
    clinic_tz = resolve_clinic_timezone(profile.timezone)
    local_date = start_at.astimezone(clinic_tz).date()
    hours = await get_hours_by_clinic_id(session, clinic_id)
    closures = await get_upcoming_closures_by_clinic_id(session, clinic_id, local_date, local_date)
    slots = compute_available_slots(
        clinic_id=clinic_id,
        service=service,
        hours=hours,
        closures=closures,
        existing_appointments=(),
        start_date=local_date,
        end_date=local_date,
        clinic_tz=clinic_tz,
        now_utc=now.astimezone(UTC),
    )
    if not any(slot.start_at == start_at.astimezone(UTC) for slot in slots):
        raise SlotUnavailableError("Requested slot is no longer available.")
