"""Unit tests for slot availability calculation, interval boundaries, and phone normalization."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.schemas.dto import (
    AppointmentDTO,
    BusinessClosureDTO,
    BusinessHoursDTO,
    ServiceDTO,
)
from app.services.availability import (
    compute_available_slots,
    get_affected_date_ints,
    intervals_overlap,
    normalize_phone,
)
from app.services.booking_errors import InvalidInputError

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def standard_service() -> ServiceDTO:
    return ServiceDTO(
        id=1,
        name="General Consultation",
        description="Standard examination",
        duration_minutes=30,
        buffer_minutes=15,
        price_inr=Decimal("500.00"),
        min_lead_time_hours=2,
        cancellation_window_hours=24,
        sort_order=1,
    )


@pytest.fixture
def standard_hours() -> tuple[BusinessHoursDTO, ...]:
    # Monday through Friday: 09:00 - 17:00, lunch 13:00 - 14:00
    # Saturday: 09:00 - 13:00, no break
    # Sunday: closed
    h = []
    for d in range(5):
        h.append(
            BusinessHoursDTO(
                day_of_week=d,
                is_closed=False,
                open_time=time(9, 0),
                close_time=time(17, 0),
                break_start=time(13, 0),
                break_end=time(14, 0),
            )
        )
    h.append(
        BusinessHoursDTO(
            day_of_week=5,
            is_closed=False,
            open_time=time(9, 0),
            close_time=time(13, 0),
            break_start=None,
            break_end=None,
        )
    )
    h.append(
        BusinessHoursDTO(
            day_of_week=6,
            is_closed=True,
            open_time=None,
            close_time=None,
            break_start=None,
            break_end=None,
        )
    )
    return tuple(h)


# =============================================================================
# Phone Normalization Tests
# =============================================================================


def test_normalize_phone_valid():
    assert normalize_phone("+91 98765-43210") == "+919876543210"
    assert normalize_phone("09876543210") == "+919876543210"
    assert normalize_phone("9876543210") == "+919876543210"
    assert normalize_phone("+919876543210") == "+919876543210"
    assert normalize_phone("919876543210") == "+919876543210"
    assert normalize_phone("+91 (6123) 456789") == "+916123456789"
    assert normalize_phone("7123456789") == "+917123456789"
    assert normalize_phone("8123456789") == "+918123456789"


def test_normalize_phone_invalid_prefixes():
    # Indian mobile numbers cannot begin with 0-5
    with pytest.raises(InvalidInputError, match="must begin with 6, 7, 8, or 9"):
        normalize_phone("5123456789")
    with pytest.raises(InvalidInputError, match="must begin with 6, 7, 8, or 9"):
        normalize_phone("1123456789")


def test_normalize_phone_unicode_digits_rejected():
    # Non-ASCII digits like Devanagari numerals must be rejected
    with pytest.raises(InvalidInputError, match="ASCII decimal digits"):
        normalize_phone("९८७६५४३२१०")


def test_normalize_phone_length_errors():
    with pytest.raises(InvalidInputError, match="expected 10 digits"):
        normalize_phone("987654321")  # 9 digits
    with pytest.raises(InvalidInputError, match="expected 10 digits"):
        normalize_phone("987654321000")  # 12 digits
    with pytest.raises(InvalidInputError, match="non-empty string"):
        normalize_phone("")


# =============================================================================
# Interval & Date Lock Tests
# =============================================================================


def test_get_affected_date_ints_ending_at_midnight():
    # Interval [2026-11-20 23:30, 2026-11-21 00:00:00) ending exactly at midnight
    start = datetime(2026, 11, 20, 23, 30, tzinfo=KOLKATA_TZ)
    end = datetime(2026, 11, 21, 0, 0, tzinfo=KOLKATA_TZ)
    dates = get_affected_date_ints(start, end, KOLKATA_TZ)
    assert dates == [20261120]


def test_get_affected_date_ints_starting_at_midnight():
    # Interval [2026-11-21 00:00:00, 2026-11-21 00:40:00)
    start = datetime(2026, 11, 21, 0, 0, tzinfo=KOLKATA_TZ)
    end = datetime(2026, 11, 21, 0, 40, tzinfo=KOLKATA_TZ)
    dates = get_affected_date_ints(start, end, KOLKATA_TZ)
    assert dates == [20261121]


def test_get_affected_date_ints_crossing_midnight():
    # Interval [2026-11-20 23:45, 2026-11-21 00:25)
    start = datetime(2026, 11, 20, 23, 45, tzinfo=KOLKATA_TZ)
    end = datetime(2026, 11, 21, 0, 25, tzinfo=KOLKATA_TZ)
    dates = get_affected_date_ints(start, end, KOLKATA_TZ)
    assert dates == [20261120, 20261121]


def test_get_affected_date_ints_multi_date():
    start = datetime(2026, 11, 20, 12, 0, tzinfo=KOLKATA_TZ)
    end = datetime(2026, 11, 22, 12, 0, tzinfo=KOLKATA_TZ)
    dates = get_affected_date_ints(start, end, KOLKATA_TZ)
    assert dates == [20261120, 20261121, 20261122]


def test_intervals_overlap_cases():
    t0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 20, 10, 30, tzinfo=UTC)
    t2 = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)

    # Disjoint touching intervals do NOT overlap: [10:00, 10:30) and [10:30, 11:00)
    assert not intervals_overlap(t0, t1, t1, t2)
    assert not intervals_overlap(t1, t2, t0, t1)

    # Overlapping intervals
    t_mid = datetime(2026, 11, 20, 10, 15, tzinfo=UTC)
    assert intervals_overlap(t0, t1, t_mid, t2)


# =============================================================================
# Slot Availability Calculation Tests
# =============================================================================


def test_slot_on_open_day(standard_service, standard_hours):
    # Monday Nov 23, 2026
    test_date = date(2026, 11, 23)
    now_utc = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)

    slots = compute_available_slots(
        clinic_id=1,
        service=standard_service,
        hours=standard_hours,
        closures=(),
        existing_appointments=[],
        start_date=test_date,
        end_date=test_date,
        clinic_tz=KOLKATA_TZ,
        now_utc=now_utc,
    )
    assert len(slots) > 0
    # First slot should start at 09:00 local time
    first_slot_local = slots[0].start_at.astimezone(KOLKATA_TZ)
    assert first_slot_local.time() == time(9, 0)
    assert slots[0].duration_minutes == 30
    assert slots[0].buffer_minutes == 15


def test_no_slot_on_closed_weekday(standard_service, standard_hours):
    # Sunday Nov 29, 2026 is closed
    sunday = date(2026, 11, 29)
    now_utc = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)

    slots = compute_available_slots(
        clinic_id=1,
        service=standard_service,
        hours=standard_hours,
        closures=(),
        existing_appointments=[],
        start_date=sunday,
        end_date=sunday,
        clinic_tz=KOLKATA_TZ,
        now_utc=now_utc,
    )
    assert len(slots) == 0


def test_no_slot_on_closure_date(standard_service, standard_hours):
    test_date = date(2026, 11, 23)
    closures = (BusinessClosureDTO(date=test_date, reason="Clinic Renovation", all_day=True),)
    now_utc = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)

    slots = compute_available_slots(
        clinic_id=1,
        service=standard_service,
        hours=standard_hours,
        closures=closures,
        existing_appointments=[],
        start_date=test_date,
        end_date=test_date,
        clinic_tz=KOLKATA_TZ,
        now_utc=now_utc,
    )
    assert len(slots) == 0


def test_slot_respects_break(standard_service, standard_hours):
    # Lunch break is 13:00 - 14:00
    test_date = date(2026, 11, 23)
    now_utc = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)

    slots = compute_available_slots(
        clinic_id=1,
        service=standard_service,
        hours=standard_hours,
        closures=(),
        existing_appointments=[],
        start_date=test_date,
        end_date=test_date,
        clinic_tz=KOLKATA_TZ,
        now_utc=now_utc,
    )

    for slot in slots:
        start_local = slot.start_at.astimezone(KOLKATA_TZ).time()
        blocked_until_local = slot.blocked_until_at.astimezone(KOLKATA_TZ).time()
        # No blocked interval can intersect 13:00 - 14:00
        # It must either end on/before 13:00, or begin on/after 14:00
        assert blocked_until_local <= time(13, 0) or start_local >= time(14, 0)


def test_buffer_cannot_overlap_break(standard_hours):
    # Service duration 40 min, buffer 30 min.
    # A slot at 12:30 ends at 13:10, and buffer extends to 13:40, penetrating the 13:00 break.
    service = ServiceDTO(
        id=2,
        name="Complex Procedure",
        description="Long procedure",
        duration_minutes=40,
        buffer_minutes=30,
        price_inr=Decimal("1500.00"),
        min_lead_time_hours=2,
        cancellation_window_hours=24,
        sort_order=2,
    )
    test_date = date(2026, 11, 23)
    now_utc = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)

    slots = compute_available_slots(
        clinic_id=1,
        service=service,
        hours=standard_hours,
        closures=(),
        existing_appointments=[],
        start_date=test_date,
        end_date=test_date,
        clinic_tz=KOLKATA_TZ,
        now_utc=now_utc,
    )
    for slot in slots:
        start_local = slot.start_at.astimezone(KOLKATA_TZ).time()
        blocked_until_local = slot.blocked_until_at.astimezone(KOLKATA_TZ).time()
        assert blocked_until_local <= time(13, 0) or start_local >= time(14, 0)


def test_buffer_cannot_extend_past_closing(standard_service, standard_hours):
    test_date = date(2026, 11, 23)
    now_utc = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)

    slots = compute_available_slots(
        clinic_id=1,
        service=standard_service,
        hours=standard_hours,
        closures=(),
        existing_appointments=[],
        start_date=test_date,
        end_date=test_date,
        clinic_tz=KOLKATA_TZ,
        now_utc=now_utc,
    )
    for slot in slots:
        blocked_until_local = slot.blocked_until_at.astimezone(KOLKATA_TZ).time()
        assert blocked_until_local <= time(17, 0)


def test_slot_respects_lead_time(standard_service, standard_hours):
    # min_lead_time_hours is 2
    test_date = date(2026, 11, 23)
    # now_utc is Nov 23 08:00 local time (02:30 UTC)
    now_utc = datetime(2026, 11, 23, 8, 0, tzinfo=KOLKATA_TZ).astimezone(UTC)

    slots = compute_available_slots(
        clinic_id=1,
        service=standard_service,
        hours=standard_hours,
        closures=(),
        existing_appointments=[],
        start_date=test_date,
        end_date=test_date,
        clinic_tz=KOLKATA_TZ,
        now_utc=now_utc,
    )
    # Earliest slot must be >= 10:00 local time
    earliest_local = slots[0].start_at.astimezone(KOLKATA_TZ).time()
    assert earliest_local >= time(10, 0)


def test_existing_appointment_blocks_slot(standard_service, standard_hours):
    test_date = date(2026, 11, 23)
    now_utc = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)

    # Book 10:00 - 10:30 with 15 min buffer (blocked until 10:45)
    apt_start = datetime(2026, 11, 23, 10, 0, tzinfo=KOLKATA_TZ).astimezone(UTC)
    apt_end = datetime(2026, 11, 23, 10, 30, tzinfo=KOLKATA_TZ).astimezone(UTC)
    apt_blocked = datetime(2026, 11, 23, 10, 45, tzinfo=KOLKATA_TZ).astimezone(UTC)

    existing = [
        AppointmentDTO(
            id=1,
            clinic_id=1,
            service_id=1,
            service_name="General Consultation",
            caller_name="Existing Patient",
            caller_phone="+919876543210",
            caller_email=None,
            start_at=apt_start,
            end_at=apt_end,
            duration_minutes=30,
            buffer_minutes=15,
            blocked_until_at=apt_blocked,
            status="confirmed",
            version=1,
            cancellation_reason=None,
            created_at=now_utc,
            updated_at=now_utc,
        )
    ]

    slots = compute_available_slots(
        clinic_id=1,
        service=standard_service,
        hours=standard_hours,
        closures=(),
        existing_appointments=existing,
        start_date=test_date,
        end_date=test_date,
        clinic_tz=KOLKATA_TZ,
        now_utc=now_utc,
    )

    # Slot at 10:00 local time must NOT be in available slots
    slot_times = [s.start_at.astimezone(KOLKATA_TZ).time() for s in slots]
    assert time(10, 0) not in slot_times
    # Next slot at 10:30 has interval [10:30, 11:15), which overlaps with existing [10:00, 10:45)!
    assert time(10, 30) not in slot_times
    # Slot at 11:00 starts after 10:45, so it should be available
    assert time(11, 0) in slot_times


def test_availability_window_too_large(standard_service, standard_hours):
    now_utc = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    with pytest.raises(InvalidInputError, match="cannot exceed 14 days"):
        compute_available_slots(
            clinic_id=1,
            service=standard_service,
            hours=standard_hours,
            closures=(),
            existing_appointments=[],
            start_date=date(2026, 11, 20),
            end_date=date(2026, 12, 10),  # > 14 days
            clinic_tz=KOLKATA_TZ,
            now_utc=now_utc,
        )
