"""Current database-backed facts are bounded and never invented."""

from datetime import date, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.dto import (
    FAQDTO,
    BusinessClosureDTO,
    BusinessHoursDTO,
    ClinicPolicyDTO,
    ServiceDTO,
)
from app.services import clinic_lookup
from app.voice.clinic_tools import ClinicVoiceTools


@pytest.fixture
def facts(monkeypatch):
    profile = SimpleNamespace(
        id=1,
        name="Demo clinic",
        description="Fictional clinic",
        timezone="Asia/Kolkata",
        address_line1="Demo street",
        address_line2=None,
        city="Delhi",
        state="Delhi",
        postal_code="110001",
        country="India",
        phone=None,
        email=None,
        website=None,
    )
    service = ServiceDTO(
        id=7,
        name="Teeth cleaning",
        description="A routine cleaning",
        duration_minutes=45,
        buffer_minutes=10,
        price_inr=Decimal("1500"),
        min_lead_time_hours=2,
        cancellation_window_hours=24,
        sort_order=0,
    )
    hours = (
        BusinessHoursDTO(
            day_of_week=0,
            is_closed=False,
            open_time=time(9),
            close_time=time(18),
            break_start=time(13),
            break_end=time(14),
        ),
    )
    mocks = {
        "get_profile_by_id": AsyncMock(return_value=profile),
        "get_active_services_by_clinic_id": AsyncMock(return_value=(service,)),
        "get_hours_by_clinic_id": AsyncMock(return_value=hours),
        "get_upcoming_closures_by_clinic_id": AsyncMock(return_value=()),
        "get_policies_by_clinic_id": AsyncMock(return_value=()),
        "get_active_faqs_by_clinic_id": AsyncMock(return_value=()),
    }
    for key, mock in mocks.items():
        monkeypatch.setattr(clinic_lookup, key, mock)
    return SimpleNamespace(profile=profile, service=service, mocks=mocks)


async def test_hours_use_dated_closures_and_breaks(facts):
    db = AsyncMock()
    closure = BusinessClosureDTO(date=date(2026, 11, 16), reason="Holiday")
    facts.mocks["get_upcoming_closures_by_clinic_id"].return_value = (closure,)
    output = await clinic_lookup.lookup_clinic_info(db, 1, "hours", "2026-11-16")
    assert "Closed on 2026-11-16: Holiday" in output
    assert "break 01:00 PM to 02:00 PM" in output
    facts.mocks["get_upcoming_closures_by_clinic_id"].assert_awaited_with(
        db, 1, closure.date, closure.date
    )
    assert "YYYY-MM-DD" in await clinic_lookup.lookup_clinic_info(db, 1, "hours", "bad")


async def test_upcoming_closures_are_explicitly_time_bounded(facts):
    result = await clinic_lookup.lookup_clinic_info(AsyncMock(), 1, "upcoming holidays")
    assert "No closures are listed from" in result and "through" in result
    _, clinic_id, start, end = facts.mocks["get_upcoming_closures_by_clinic_id"].await_args.args
    assert clinic_id == 1 and (end - start).days == 30


async def test_policy_faq_unknown_and_ambiguous_facts(facts):
    facts.mocks["get_policies_by_clinic_id"].return_value = (
        ClinicPolicyDTO(
            policy_type="cancellation",
            title="Cancellation",
            content="Give one day notice.",
            sort_order=0,
        ),
    )
    facts.mocks["get_active_faqs_by_clinic_id"].return_value = (
        FAQDTO(
            id=1,
            topic="parking",
            question="Is parking available?",
            answer="Parking is available.",
            sort_order=0,
        ),
    )
    db = AsyncMock()
    assert "one day notice" in await clinic_lookup.lookup_clinic_info(db, 1, "cancellation policy")
    assert "Parking is available" in await clinic_lookup.lookup_clinic_info(
        db, 1, "Is parking available?"
    )
    assert "Parking is available" in await clinic_lookup.lookup_clinic_info(
        db, 1, "Where can I find parking near the clinic?"
    )
    assert "do not have" in await clinic_lookup.lookup_clinic_info(db, 1, "insurance")
    facts.mocks["get_active_faqs_by_clinic_id"].return_value += (
        FAQDTO(id=2, topic="parking", question="Where to park?", answer="Elsewhere.", sort_order=1),
    )
    assert "clarify" in await clinic_lookup.lookup_clinic_info(db, 1, "parking")


async def test_active_services_prices_details_and_fresh_results(facts):
    db = AsyncMock()
    result = await clinic_lookup.lookup_clinic_services(db, 1)
    assert "Teeth cleaning" in result and "INR 1500.00" in result and "45 minutes" in result
    assert "routine cleaning" in await clinic_lookup.lookup_clinic_services(db, 1, "Teeth cleaning")
    facts.mocks["get_active_services_by_clinic_id"].return_value = ()
    assert "No services" in await clinic_lookup.lookup_clinic_services(db, 1)
    assert facts.mocks["get_active_services_by_clinic_id"].await_count == 3


async def test_oversized_fact_fails_closed(facts):
    facts.mocks["get_policies_by_clinic_id"].return_value = (
        ClinicPolicyDTO(
            policy_type="cancellation", title="Cancellation", content="x" * 4000, sort_order=0
        ),
    )
    result = await clinic_lookup.lookup_clinic_info(AsyncMock(), 1, "cancellation")
    assert "too long" in result and "x" * 100 not in result


async def test_tool_database_failure_is_safe_and_traced(monkeypatch):
    from app.voice import clinic_tools

    lookup = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(clinic_tools, "lookup_clinic_info", lookup)
    trace = MagicMock()
    tools = ClinicVoiceTools(
        MagicMock(),
        MagicMock(),
        SimpleNamespace(profile=SimpleNamespace(id=1)),
        "sess-abcdef123456",
        "client",
        trace=trace,
    )
    wrapper = next(tool for tool in tools.get_tools() if tool.__name__ == "get_clinic_info")
    result = await wrapper(None, topic="hours")
    assert "temporarily unavailable" in result
    assert trace.record_tool.call_args.args[1] == "failed"


async def test_service_list_limits_output_and_marks_omissions(facts):
    from dataclasses import replace

    facts.mocks["get_active_services_by_clinic_id"].return_value = tuple(
        replace(facts.service, id=i, name=f"Service {i}") for i in range(40)
    )
    result = await clinic_lookup.lookup_clinic_services(AsyncMock(), 1)
    assert "Additional services are not shown" in result
    assert len(result) <= clinic_lookup.MAX_FACT_CHARS
    assert "clarify" in await clinic_lookup.lookup_clinic_services(AsyncMock(), 1, "Service")


async def test_availability_rejects_extreme_dates_before_database_access():
    factory = MagicMock()
    tools = ClinicVoiceTools(MagicMock(), factory, MagicMock(), "sess-abcdef123456", "client")
    result = await tools.check_availability(None, 1, "9999-12-31", 14)
    assert "valid start date" in result
    factory.assert_not_called()
