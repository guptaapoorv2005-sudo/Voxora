"""Availability calculation, interval math, and phone normalization.

Pure functional services calculating valid appointment slots based on operating hours,
lunch breaks, scheduled closures, service duration, post-service buffers, minimum lead times,
and existing confirmed appointments.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import Appointment
from app.schemas.dto import (
    BusinessClosureDTO,
    BusinessHoursDTO,
    ServiceDTO,
    SlotDTO,
)
from app.services.booking_errors import InvalidInputError

ASCII_PHONE_CLEAN_PATTERN = re.compile(r"^[0-9]+$")
INDIAN_MOBILE_ASCII_PATTERN = re.compile(r"^[6-9][0-9]{9}$")


class AppointmentInterval(Protocol):
    @property
    def status(self) -> str: ...

    @property
    def start_at(self) -> datetime: ...

    @property
    def blocked_until_at(self) -> datetime: ...


def normalize_phone(raw: str) -> str:
    """Normalize and validate an Indian mobile phone number to canonical E.164 (+91XXXXXXXXXX).

    Rules:
    - Strips spaces, dashes, dots, parentheses, and leading zeros.
    - Strips optional leading '+91' or '91' prefix.
    - Strictly checks that the remaining 10 digits are ASCII decimal digits (0-9).
    - Requires the first digit to be in [6, 7, 8, 9].
    - Returns standardized E.164 format: '+91XXXXXXXXXX'.
    """
    if not raw or not isinstance(raw, str):
        raise InvalidInputError("Phone number must be a non-empty string.")

    # Remove all formatting punctuation and spaces
    cleaned = re.sub(r"[\s\-\.\(\)]+", "", raw.strip())

    # Handle leading '+'
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]

    # Remove leading country code 91 if present and length indicates country code
    if cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = cleaned[2:]

    # Remove leading trunk zero if present
    if cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = cleaned[1:]

    # Verify strictly 10 digits
    if len(cleaned) != 10:
        raise InvalidInputError(
            f"Invalid phone number length: expected 10 digits, got {len(cleaned)}."
        )

    # Verify ASCII digits only (rejecting Unicode numbers)
    if not ASCII_PHONE_CLEAN_PATTERN.fullmatch(cleaned):
        raise InvalidInputError("Phone number must contain only ASCII decimal digits (0-9).")

    # Verify valid Indian mobile prefix (6, 7, 8, 9)
    if not INDIAN_MOBILE_ASCII_PATTERN.fullmatch(cleaned):
        raise InvalidInputError("Invalid Indian mobile number: must begin with 6, 7, 8, or 9.")

    return f"+91{cleaned}"


def get_affected_date_ints(
    start_at: datetime,
    blocked_until_at: datetime,
    clinic_tz: tzinfo,
) -> list[int]:
    """Return all unique clinic-local calendar dates spanned by half-open [start_at, blocked_until_at).

    Boundary semantics:
    - [2026-11-20 23:30, 2026-11-21 00:00:00) ending exactly at midnight locks ONLY [20261120].
    - [2026-11-20 23:45, 2026-11-21 00:25:00) crossing midnight locks [20261120, 20261121].
    """
    if start_at.tzinfo is None or blocked_until_at.tzinfo is None:
        raise ValueError("start_at and blocked_until_at must be timezone-aware datetimes.")
    if blocked_until_at <= start_at:
        raise ValueError("blocked_until_at must be strictly greater than start_at.")

    start_local = start_at.astimezone(clinic_tz).date()
    # Subtract 1 microsecond so an interval ending exactly at midnight does not lock the next day
    end_local = (blocked_until_at - timedelta(microseconds=1)).astimezone(clinic_tz).date()

    dates = set()
    curr = start_local
    while curr <= end_local:
        dates.add(int(curr.strftime("%Y%m%d")))
        curr += timedelta(days=1)
    return sorted(dates)


def intervals_overlap(
    start_a: datetime,
    blocked_until_a: datetime,
    start_b: datetime,
    blocked_until_b: datetime,
) -> bool:
    """Return True if two half-open intervals [start_a, blocked_until_a) and [start_b, blocked_until_b) overlap."""
    return start_a < blocked_until_b and start_b < blocked_until_a


def compute_available_slots(
    *,
    clinic_id: int,
    service: ServiceDTO,
    hours: tuple[BusinessHoursDTO, ...],
    closures: tuple[BusinessClosureDTO, ...],
    existing_appointments: Sequence[AppointmentInterval],
    start_date: date,
    end_date: date,
    clinic_tz: tzinfo,
    now_utc: datetime,
    slot_interval_minutes: int = 30,
) -> list[SlotDTO]:
    """Compute all available appointment slots for a given service and date window.

    Enforces:
    - Maximum 14-day search window.
    - Scheduled clinic closures.
    - Day-of-week operating hours.
    - Lunch break exclusions (buffer cannot overlap lunch break).
    - Closing time exclusions (buffer cannot extend beyond close).
    - Minimum lead time from now_utc.
    - Existing confirmed appointment blocked intervals.
    """
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be a timezone-aware UTC datetime.")
    if end_date < start_date:
        raise InvalidInputError("end_date cannot precede start_date.")
    if (end_date - start_date).days > 14:
        raise InvalidInputError("Availability window cannot exceed 14 days.")

    closure_dates = {c.date for c in closures if c.all_day}
    hours_by_day = {h.day_of_week: h for h in hours}

    min_start_utc = now_utc + timedelta(hours=service.min_lead_time_hours)
    available_slots: list[SlotDTO] = []

    curr_date = start_date
    while curr_date <= end_date:
        if curr_date in closure_dates:
            curr_date += timedelta(days=1)
            continue

        day_hours = hours_by_day.get(curr_date.weekday())
        if (
            not day_hours
            or day_hours.is_closed
            or not day_hours.open_time
            or not day_hours.close_time
        ):
            curr_date += timedelta(days=1)
            continue

        # Candidate slot generation on grid
        slot_local = datetime.combine(curr_date, day_hours.open_time, tzinfo=clinic_tz)
        day_close_local = datetime.combine(curr_date, day_hours.close_time, tzinfo=clinic_tz)

        break_start_local = (
            datetime.combine(curr_date, day_hours.break_start, tzinfo=clinic_tz)
            if day_hours.break_start
            else None
        )
        break_end_local = (
            datetime.combine(curr_date, day_hours.break_end, tzinfo=clinic_tz)
            if day_hours.break_end
            else None
        )

        while True:
            slot_end_local = slot_local + timedelta(minutes=service.duration_minutes)
            blocked_until_local = slot_end_local + timedelta(minutes=service.buffer_minutes)

            # Check 1: Must conclude before or at closing time
            if blocked_until_local > day_close_local:
                break

            # Check 2: Break overlap
            # Entire blocked interval must conclude <= break_start OR begin >= break_end
            if break_start_local and break_end_local:
                if intervals_overlap(
                    slot_local, blocked_until_local, break_start_local, break_end_local
                ):
                    slot_local += timedelta(minutes=slot_interval_minutes)
                    continue

            slot_start_utc = slot_local.astimezone(UTC)
            slot_end_utc = slot_end_local.astimezone(UTC)
            blocked_until_utc = blocked_until_local.astimezone(UTC)

            # Check 3: Lead time
            if slot_start_utc < min_start_utc:
                slot_local += timedelta(minutes=slot_interval_minutes)
                continue

            # Check 4: Conflict with existing confirmed appointments
            has_conflict = False
            for apt in existing_appointments:
                if apt.status == "confirmed" and intervals_overlap(
                    slot_start_utc, blocked_until_utc, apt.start_at, apt.blocked_until_at
                ):
                    has_conflict = True
                    break

            if not has_conflict:
                available_slots.append(
                    SlotDTO(
                        start_at=slot_start_utc,
                        end_at=slot_end_utc,
                        duration_minutes=service.duration_minutes,
                        buffer_minutes=service.buffer_minutes,
                        blocked_until_at=blocked_until_utc,
                    )
                )

            slot_local += timedelta(minutes=slot_interval_minutes)

        curr_date += timedelta(days=1)

    return available_slots


async def check_appointment_conflict(
    session: AsyncSession,
    *,
    clinic_id: int,
    start_at: datetime,
    duration_minutes: int,
    buffer_minutes: int,
    exclude_appointment_id: int | None = None,
) -> bool:
    """Check whether a candidate interval overlaps with any existing confirmed appointment in PostgreSQL.

    Candidate interval: [start_at, blocked_until_at).
    Uses the persisted blocked_until_at of existing appointments.
    """
    if start_at.tzinfo is None or start_at.utcoffset() is None:
        raise ValueError("start_at must be a timezone-aware UTC datetime.")

    blocked_until_at = start_at + timedelta(minutes=duration_minutes + buffer_minutes)

    # Condition for overlap: appointment.start_at < blocked_until_at AND appointment.blocked_until_at > start_at
    stmt = (
        select(Appointment.id)
        .where(
            Appointment.clinic_id == clinic_id,
            Appointment.status == "confirmed",
            Appointment.start_at < blocked_until_at,
            Appointment.blocked_until_at > start_at,
        )
        .limit(1)
    )

    if exclude_appointment_id is not None:
        stmt = stmt.where(Appointment.id != exclude_appointment_id)

    res = await session.execute(stmt)
    return res.scalar_one_or_none() is not None


async def acquire_calendar_advisory_locks(
    session: AsyncSession,
    clinic_id: int,
    start_at: datetime,
    blocked_until_at: datetime,
    clinic_tz: tzinfo,
) -> list[int]:
    """Acquire transaction-level PostgreSQL advisory locks for all calendar dates spanned by [start_at, blocked_until_at).

    Locks are acquired in strictly ascending date order to prevent deadlocks.
    Transaction-level advisory locks automatically release on COMMIT or ROLLBACK.
    """
    date_ints = get_affected_date_ints(start_at, blocked_until_at, clinic_tz)
    for d_int in date_ints:
        await session.execute(select(func.pg_advisory_xact_lock(clinic_id, d_int)))
    return date_ints


async def acquire_reschedule_calendar_advisory_locks(
    session: AsyncSession,
    clinic_id: int,
    old_start_at: datetime,
    old_blocked_until_at: datetime,
    new_start_at: datetime,
    new_blocked_until_at: datetime,
    clinic_tz: tzinfo,
) -> list[int]:
    """Acquire transaction-level PostgreSQL advisory locks for union of old and new calendar dates.

    Locks are acquired in strictly ascending date order to prevent deadlocks.
    Transaction-level advisory locks automatically release on COMMIT or ROLLBACK.
    """
    old_dates = set(get_affected_date_ints(old_start_at, old_blocked_until_at, clinic_tz))
    new_dates = set(get_affected_date_ints(new_start_at, new_blocked_until_at, clinic_tz))
    union_dates = sorted(old_dates | new_dates)
    for d_int in union_dates:
        await session.execute(select(func.pg_advisory_xact_lock(clinic_id, d_int)))
    return union_dates
