from datetime import UTC, date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.schemas.dto import BusinessClosureDTO, BusinessHoursDTO, ServiceDTO
from app.services import schedule_validation as schedule
from app.services.booking_errors import SlotUnavailableError


@pytest.fixture
def clinic(monkeypatch):
    service = ServiceDTO(
        id=1,
        name="Checkup",
        description=None,
        duration_minutes=30,
        buffer_minutes=15,
        price_inr=Decimal("500"),
        min_lead_time_hours=2,
        cancellation_window_hours=24,
        sort_order=0,
    )
    hours = BusinessHoursDTO(
        day_of_week=0,
        is_closed=False,
        open_time=time(9),
        close_time=time(18),
        break_start=time(13),
        break_end=time(14),
    )
    monkeypatch.setattr(
        schedule,
        "get_profile_by_id",
        AsyncMock(return_value=SimpleNamespace(timezone="Asia/Kolkata")),
    )
    monkeypatch.setattr(
        schedule, "get_active_services_by_clinic_id", AsyncMock(return_value=(service,))
    )
    monkeypatch.setattr(schedule, "get_hours_by_clinic_id", AsyncMock(return_value=(hours,)))
    monkeypatch.setattr(schedule, "get_upcoming_closures_by_clinic_id", AsyncMock(return_value=()))
    return service


def args(start):
    return dict(
        clinic_id=1,
        service_id=1,
        start_at=start,
        duration_minutes=30,
        buffer_minutes=15,
        service_name="Checkup",
        now=datetime(2026, 11, 15, tzinfo=UTC),
    )


async def test_valid_slot_uses_clinic_local_time_and_grid(clinic):
    start = datetime(2026, 11, 16, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    await schedule.validate_schedule_slot(AsyncMock(), **args(start))


@pytest.mark.parametrize("hour,minute", [(8, 30), (12, 30), (17, 30), (10, 15)])
async def test_closed_break_buffer_and_off_grid_rejected(clinic, hour, minute):
    start = datetime(2026, 11, 16, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata"))
    with pytest.raises(SlotUnavailableError):
        await schedule.validate_schedule_slot(AsyncMock(), **args(start))


async def test_closure_and_changed_service_rejected(clinic):
    start = datetime(2026, 11, 16, 10, tzinfo=ZoneInfo("Asia/Kolkata"))
    schedule.get_upcoming_closures_by_clinic_id.return_value = (
        BusinessClosureDTO(date(2026, 11, 16), "Holiday"),
    )
    with pytest.raises(SlotUnavailableError):
        await schedule.validate_schedule_slot(AsyncMock(), **args(start))
    schedule.get_upcoming_closures_by_clinic_id.return_value = ()
    values = args(start)
    values["duration_minutes"] = 45
    with pytest.raises(SlotUnavailableError):
        await schedule.validate_schedule_slot(AsyncMock(), **values)
    schedule.get_active_services_by_clinic_id.return_value = ()
    with pytest.raises(SlotUnavailableError):
        await schedule.validate_schedule_slot(AsyncMock(), **args(start))


async def test_lead_time_is_rechecked(clinic):
    tz = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 11, 16, 10, tzinfo=tz)
    values = args(start)
    values["now"] = datetime(2026, 11, 16, 9, tzinfo=tz)
    with pytest.raises(SlotUnavailableError):
        await schedule.validate_schedule_slot(AsyncMock(), **values)


async def test_voice_rejects_invalid_slot_before_creating_a_proposal(monkeypatch):
    from app.services import voice_booking_service as voice

    db = AsyncMock()
    db.add = MagicMock()
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = None
    service_result = MagicMock()
    service_result.scalar_one_or_none.return_value = SimpleNamespace(
        duration_minutes=30, buffer_minutes=15, name="Checkup"
    )
    db.execute.side_effect = [session_result, service_result]
    manager = AsyncMock()
    manager.__aenter__.return_value = db

    def factory():
        return manager

    validation = AsyncMock(side_effect=SlotUnavailableError("Closed"))
    monkeypatch.setattr(voice, "validate_schedule_slot", validation)
    service = voice.VoiceBookingService(factory, signing_secret="a" * 32)
    with pytest.raises(SlotUnavailableError):
        await service.create_booking_proposal(
            voice_session_id="sess-example",
            service_id=1,
            start_at="2026-11-16T10:00:00+05:30",
            caller_name="Test",
            caller_phone="9876543210",
        )
    validation.assert_awaited_once()
    db.add.assert_not_called()
