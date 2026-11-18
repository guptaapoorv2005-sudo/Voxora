"""Unit tests for clinic context assembly and formatting.

These tests run completely offline and verify:
1. Timezone conversion and business hours formatting.
2. Holiday closure overrides over regular business hours.
3. Deterministic date formatting with fixed injected instants.
4. Timezone-naive datetime rejection.
5. Strict 4,000-character context ceiling enforcement.
6. Deterministic FAQ shedding when context exceeds ceiling.
7. Explicit failure escalation when core content exceeds ceiling.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from app.schemas.dto import (
    FAQDTO,
    BusinessClosureDTO,
    BusinessHoursDTO,
    ClinicContextDTO,
    ClinicPolicyDTO,
    ClinicProfileDTO,
    ServiceDTO,
)
from app.services.clinic_context import (
    ClinicNotFoundError,
    ContextSizeLimitExceededError,
    build_clinic_prompt_context,
    load_clinic_context,
)
from app.voice.context import resolve_clinic_timezone

_IST = resolve_clinic_timezone("Asia/Kolkata")


def _build_test_profile(
    phone: str | None = None,
    timezone: str = "Asia/Kolkata",
) -> ClinicProfileDTO:
    return ClinicProfileDTO(
        id=1,
        slug="brightsmile",
        name="BrightSmile Dental Clinic",
        description="A neighborhood dental clinic offering routine dental consultations.",
        address_line1="24 Green Park Main Market",
        address_line2=None,
        city="New Delhi",
        state="Delhi",
        postal_code="110016",
        country="India",
        phone=phone,
        email="contact@brightsmiledental.example",
        website="https://brightsmile.example",
        timezone=timezone,
    )


def _build_test_hours() -> tuple[BusinessHoursDTO, ...]:
    # Mon-Sat 09:00-18:00 (break 13:00-14:00), Sun closed
    hours: list[BusinessHoursDTO] = []
    for day in range(6):
        hours.append(
            BusinessHoursDTO(
                day_of_week=day,
                is_closed=False,
                open_time=time(9, 0),
                close_time=time(18, 0),
                break_start=time(13, 0),
                break_end=time(14, 0),
            )
        )
    hours.append(
        BusinessHoursDTO(
            day_of_week=6,
            is_closed=True,
            open_time=None,
            close_time=None,
            break_start=None,
            break_end=None,
        )
    )
    return tuple(hours)


def _build_test_services() -> tuple[ServiceDTO, ...]:
    return (
        ServiceDTO(
            id=1,
            name="Dental consultation",
            description="Initial exam.",
            duration_minutes=30,
            buffer_minutes=10,
            price_inr=Decimal("500.00"),
            min_lead_time_hours=2,
            cancellation_window_hours=24,
            sort_order=1,
        ),
        ServiceDTO(
            id=2,
            name="Teeth cleaning",
            description="Professional scale and polish.",
            duration_minutes=45,
            buffer_minutes=10,
            price_inr=Decimal("1500.00"),
            min_lead_time_hours=2,
            cancellation_window_hours=24,
            sort_order=2,
        ),
    )


def _build_test_policies() -> tuple[ClinicPolicyDTO, ...]:
    return (
        ClinicPolicyDTO(
            policy_type="cancellation",
            title="Cancellation Policy",
            content="Appointments may be cancelled up to 24 hours in advance at no charge.",
            sort_order=1,
        ),
    )


def _build_test_faqs() -> tuple[FAQDTO, ...]:
    return (
        FAQDTO(
            id=1,
            topic="location",
            question="Where is the clinic located?",
            answer="24 Green Park Main Market, New Delhi.",
            sort_order=1,
        ),
        FAQDTO(
            id=2,
            topic="hours",
            question="What are your hours?",
            answer="Mon-Sat 9 AM to 6 PM.",
            sort_order=2,
        ),
    )


def test_today_hours_regular_open() -> None:
    # 2026-11-09 is a Monday
    fixed_monday = datetime(2026, 11, 9, 10, 0, tzinfo=_IST)
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=_build_test_faqs(),
        local_now=fixed_monday,
    )

    prompt = build_clinic_prompt_context(ctx)
    assert "Today's Status (Monday): 9:00 AM to 6:00 PM (lunch break 1:00 PM to 2:00 PM)" in prompt
    assert "BrightSmile Dental Clinic" in prompt
    assert "Dental consultation" in prompt


def test_today_hours_sunday_closed() -> None:
    # 2026-11-15 is a Sunday
    fixed_sunday = datetime(2026, 11, 15, 12, 0, tzinfo=_IST)
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=_build_test_faqs(),
        local_now=fixed_sunday,
    )

    prompt = build_clinic_prompt_context(ctx)
    assert "Today's Status (Sunday): Closed" in prompt


def test_closure_overrides_business_hours() -> None:
    # 2026-01-26 is a Monday, but Republic Day closure
    fixed_holiday = datetime(2026, 1, 26, 10, 0, tzinfo=_IST)
    closures = (
        BusinessClosureDTO(
            date=date(2026, 1, 26),
            reason="Republic Day",
            all_day=True,
        ),
    )
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=closures,
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=_build_test_faqs(),
        local_now=fixed_holiday,
    )

    prompt = build_clinic_prompt_context(ctx)
    assert "Today's Status (Monday): Closed for holiday (Republic Day)" in prompt


def test_deterministic_timestamp_formatting() -> None:
    # Ensure fixed time produces identical output across calls
    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(
            BusinessClosureDTO(date=date(2026, 11, 20), reason="Local Festival", all_day=True),
        ),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=_build_test_faqs(),
        local_now=fixed_dt,
    )

    out1 = build_clinic_prompt_context(ctx)
    out2 = build_clinic_prompt_context(ctx)
    assert out1 == out2
    assert "20 November 2026: Local Festival" in out1


def test_context_size_within_safety_limits() -> None:
    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=_build_test_faqs(),
        local_now=fixed_dt,
    )

    prompt = build_clinic_prompt_context(ctx, max_chars=4000)
    assert len(prompt) < 2500
    assert len(prompt) <= 4000


def test_dto_collections_are_genuinely_immutable() -> None:
    """Verify that ClinicContextDTO collections are tuples and cannot be mutated."""
    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=_build_test_faqs(),
        local_now=fixed_dt,
    )

    assert isinstance(ctx.hours, tuple)
    assert isinstance(ctx.closures, tuple)
    assert isinstance(ctx.services, tuple)
    assert isinstance(ctx.policies, tuple)
    assert isinstance(ctx.faqs, tuple)

    with pytest.raises(AttributeError):
        ctx.hours.append(  # type: ignore[attr-defined]
            BusinessHoursDTO(0, False, None, None, None, None)
        )


def test_empty_faqs_omits_faq_heading() -> None:
    """Verify that when no FAQs exist, no empty FAQ section heading is rendered."""
    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=(),
        local_now=fixed_dt,
    )

    prompt = build_clinic_prompt_context(ctx)
    assert "Frequently Asked Questions" not in prompt


def test_faq_shedding_large_faq_skipped_but_subsequent_small_faq_fits() -> None:
    """Verify that a large FAQ that doesn't fit is shed while a subsequent smaller FAQ fits."""
    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)

    faq_small_1 = FAQDTO(
        id=1,
        topic="small1",
        question="Small Q1?",
        answer="Short A1.",
        sort_order=1,
    )
    faq_large_2 = FAQDTO(
        id=2,
        topic="large2",
        question="Large Q2 with a lot of detail?",
        answer=(
            "Very long answer containing lots of text that cannot possibly fit in a tight budget."
            * 10
        ),
        sort_order=2,
    )
    faq_small_3 = FAQDTO(
        id=3,
        topic="small3",
        question="Small Q3?",
        answer="Short A3.",
        sort_order=3,
    )

    ctx_base = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=(),
        local_now=fixed_dt,
    )
    core_len = len(build_clinic_prompt_context(ctx_base))

    # Calculate budget: core + FAQ heading + small1 + small3 + padding, but less than large2
    small1_rendered = len("\n\nFrequently Asked Questions:\n  - Q: Small Q1?\n    A: Short A1.")
    small3_rendered = len("\n  - Q: Small Q3?\n    A: Short A3.")
    target_budget = core_len + small1_rendered + small3_rendered + 20

    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=(faq_small_1, faq_large_2, faq_small_3),
        local_now=fixed_dt,
    )

    prompt = build_clinic_prompt_context(ctx, max_chars=target_budget)
    assert len(prompt) <= target_budget
    assert "Small Q1?" in prompt
    assert "Small Q3?" in prompt
    assert "Large Q2 with a lot of detail?" not in prompt


def test_faq_shedding_multiple_priorities_preserved() -> None:
    """Verify that FAQs with multiple sort_orders are evaluated strictly in priority order."""
    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)

    faq_p1 = FAQDTO(
        id=10, topic="p1", question="Priority 1 Question?", answer="Answer 1.", sort_order=1
    )
    faq_p2 = FAQDTO(
        id=20, topic="p2", question="Priority 2 Question?", answer="Answer 2.", sort_order=2
    )
    faq_p3 = FAQDTO(
        id=30, topic="p3", question="Priority 3 Question?", answer="Answer 3.", sort_order=3
    )

    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=(faq_p3, faq_p1, faq_p2),  # passed out of order
        local_now=fixed_dt,
    )

    prompt = build_clinic_prompt_context(ctx)
    # Order in output must be Priority 1, then Priority 2, then Priority 3
    pos1 = prompt.find("Priority 1 Question?")
    pos2 = prompt.find("Priority 2 Question?")
    pos3 = prompt.find("Priority 3 Question?")
    assert -1 < pos1 < pos2 < pos3


def test_repeated_calls_produce_identical_output() -> None:
    """Verify repeated calls with identical DTO inputs produce identical output."""
    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=_build_test_faqs(),
        local_now=fixed_dt,
    )

    runs = [build_clinic_prompt_context(ctx, max_chars=4000) for _ in range(5)]
    assert all(r == runs[0] for r in runs)


def test_oversized_context_sheds_faqs_deterministically() -> None:
    # Create many synthetic FAQs to push context length past max_chars
    many_faqs: list[FAQDTO] = []
    for i in range(50):
        many_faqs.append(
            FAQDTO(
                id=i,
                topic=f"topic_{i}",
                question=f"Synthetic Question Number {i:02d} regarding detailed clinic policies?",
                answer=(
                    f"Synthetic Answer {i:02d} containing substantial text to occupy context space."
                ),
                sort_order=i,
            )
        )

    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=tuple(many_faqs),
        local_now=fixed_dt,
    )

    prompt = build_clinic_prompt_context(ctx, max_chars=2000)
    assert len(prompt) <= 2000
    # Core identity and services must still be preserved
    assert "BrightSmile Dental Clinic" in prompt
    assert "Dental consultation" in prompt
    # Lowest priority FAQs should have been shed, but earlier ones may remain
    assert "Synthetic Question Number 00" in prompt
    assert "Synthetic Question Number 49" not in prompt


def test_oversized_core_context_raises_error() -> None:
    fixed_dt = datetime(2026, 11, 14, 11, 30, tzinfo=_IST)
    # Artificially set max_chars lower than the minimal core text size
    ctx = ClinicContextDTO(
        profile=_build_test_profile(),
        hours=_build_test_hours(),
        closures=(),
        services=_build_test_services(),
        policies=_build_test_policies(),
        faqs=(),
        local_now=fixed_dt,
    )

    with pytest.raises(ContextSizeLimitExceededError, match="Core clinic context length"):
        build_clinic_prompt_context(ctx, max_chars=200)


@pytest.mark.asyncio
async def test_naive_datetime_raises_value_error() -> None:
    naive_dt = datetime(2026, 11, 14, 10, 0)  # No tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        await load_clinic_context(session=None, slug="brightsmile", now=naive_dt)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_missing_clinic_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    mock_session = AsyncMock()
    # Mock get_profile_by_slug to return None
    monkeypatch.setattr(
        "app.services.clinic_context.get_profile_by_slug",
        AsyncMock(return_value=None),
    )

    with pytest.raises(ClinicNotFoundError, match="slug 'unknown' not found"):
        await load_clinic_context(
            session=mock_session,
            slug="unknown",
            now=datetime.now(UTC),
        )
