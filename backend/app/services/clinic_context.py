"""Clinic context assembly service for the Voxora voice receptionist.

Why a dedicated context assembly service?
In our voice architecture, the LLM must NOT be given a massive unformatted JSON dump or
hardcoded system prompt. Instead:
1. It queries structured database records via `app.repositories.clinic_repo`.
2. It maps them to immutable `ClinicContextDTO`s.
3. It converts timestamps to the clinic's local timezone (`BusinessProfile.timezone`).
4. It checks whether today is a scheduled closure date, overriding regular opening hours.
5. It formats the facts into a clean, spoken-friendly markdown/plain-text block.
6. It strictly enforces a 4,000-character ceiling: if the context exceeds 4,000 characters,
   it deterministically sheds lower-priority FAQs first, logging any shedding externally
   via Python logging (never inserting truncation notes into the prompt itself).
   If core sections still exceed 4,000 characters, it raises `ContextSizeLimitExceededError`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.clinic_repo import (
    get_active_faqs_by_clinic_id,
    get_active_services_by_clinic_id,
    get_hours_by_clinic_id,
    get_policies_by_clinic_id,
    get_profile_by_slug,
    get_upcoming_closures_by_clinic_id,
)
from app.schemas.dto import (
    FAQDTO,
    BusinessClosureDTO,
    BusinessHoursDTO,
    ClinicContextDTO,
)
from app.voice.context import resolve_clinic_timezone

logger = logging.getLogger("voxora.services.clinic_context")

# Canonical weekday names (0=Monday through 6=Sunday)
_WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


class ClinicNotFoundError(Exception):
    """Raised when the requested clinic slug does not exist in the database."""


class ContextSizeLimitExceededError(ValueError):
    """Raised when core clinic context exceeds the configured character ceiling."""


def _format_time_12h(t: datetime | time) -> str:
    """Format time into a concise spoken 12-hour format (e.g. '9:00 AM', '1:30 PM')."""
    return t.strftime("%I:%M %p").lstrip("0")


def _format_hours_entry(h: BusinessHoursDTO) -> str:
    """Format a single BusinessHoursDTO into a concise spoken string."""
    if h.is_closed or h.open_time is None or h.close_time is None:
        return "Closed"

    base = f"{_format_time_12h(h.open_time)} to {_format_time_12h(h.close_time)}"
    if h.break_start is not None and h.break_end is not None:
        b_start = _format_time_12h(h.break_start)
        b_end = _format_time_12h(h.break_end)
        base += f" (lunch break {b_start} to {b_end})"
    return base


def build_clinic_prompt_context(
    ctx: ClinicContextDTO,
    max_chars: int = 4000,
) -> str:
    """Assemble a bounded, formatted plain-text clinic context block from immutable DTOs.

    Deterministic Prioritization:
    - Priority 1 (Core Identity, Hours & Closures)
    - Priority 2 (Services & Spoken Policies)
    - Priority 3 (Curated FAQs - shed in reverse priority order if max_chars is exceeded)

    Args:
        ctx: Immutable `ClinicContextDTO` loaded from repository.
        max_chars: Hard character ceiling for the context block (defaults to 4,000).

    Returns:
        Formatted context string guaranteed to be <= max_chars.

    Raises:
        ContextSizeLimitExceededError: If the core sections (Priorities 1 & 2) alone
            exceed max_chars.
    """
    profile = ctx.profile
    local_now = ctx.local_now
    today_date = local_now.date()
    today_weekday = local_now.weekday()

    # --- Priority 1: Core Clinic Identity & Today's Status ---
    addr_str = (
        f"{profile.address_line1}, {profile.city}, "
        f"{profile.state} {profile.postal_code}, {profile.country}"
    )
    identity_lines = [
        f"Clinic: {profile.name}",
        f"Description: {profile.description}",
        f"Address: {addr_str}",
        f"Timezone: {profile.timezone}",
    ]
    if profile.phone:
        identity_lines.append(f"Phone: {profile.phone}")
    else:
        identity_lines.append(
            "Contact Note: Callers can reach clinic reception via the front desk or web portal."
        )

    # Today's status (check closures first, then regular hours)
    today_closure: BusinessClosureDTO | None = next(
        (c for c in ctx.closures if c.date == today_date), None
    )
    today_hours_dto: BusinessHoursDTO | None = next(
        (h for h in ctx.hours if h.day_of_week == today_weekday), None
    )

    if today_closure is not None:
        today_status_str = f"Closed for holiday ({today_closure.reason})"
    elif today_hours_dto is not None and not today_hours_dto.is_closed:
        today_status_str = _format_hours_entry(today_hours_dto)
    else:
        today_status_str = "Closed"

    priority_1_lines = [
        *identity_lines,
        f"Today's Status ({_WEEKDAY_NAMES[today_weekday]}): {today_status_str}",
    ]

    # --- Priority 2: Weekly Schedule, Closures, Services & Spoken Policies ---
    schedule_lines = ["Regular Weekly Hours:"]
    for h in sorted(ctx.hours, key=lambda x: x.day_of_week):
        schedule_lines.append(f"  - {_WEEKDAY_NAMES[h.day_of_week]}: {_format_hours_entry(h)}")

    # Upcoming closures (next 30 days, excluding today)
    future_closures = [c for c in ctx.closures if c.date > today_date]
    if future_closures:
        schedule_lines.append("Upcoming Closures & Holidays:")
        for c in sorted(future_closures, key=lambda x: x.date):
            schedule_lines.append(f"  - {c.date.strftime('%d %B %Y')}: {c.reason}")

    services_lines = ["Offered Services:"]
    for s in sorted(ctx.services, key=lambda x: (x.sort_order, x.name)):
        price_str = f"INR {s.price_inr:.2f}" if s.price_inr is not None else "Pricing on inquiry"
        desc_str = f" - {s.description}" if s.description else ""
        services_lines.append(f"  - {s.name}: {s.duration_minutes} mins, {price_str}{desc_str}")

    policies_lines = ["Clinic Policies:"]
    for p in sorted(ctx.policies, key=lambda x: x.sort_order):
        policies_lines.append(f"  - {p.title}: {p.content}")

    core_blocks = [
        "\n".join(priority_1_lines),
        "\n".join(schedule_lines),
        "\n".join(services_lines),
        "\n".join(policies_lines),
    ]
    core_text = "\n\n".join(core_blocks)

    if len(core_text) > max_chars:
        raise ContextSizeLimitExceededError(
            f"Core clinic context length ({len(core_text)}) exceeds limit of {max_chars} chars."
        )

    # --- Priority 3: Curated FAQs (Subject to deterministic shedding) ---
    sorted_faqs: list[FAQDTO] = sorted(ctx.faqs, key=lambda x: (x.sort_order, x.id))

    def _render_context_with_faqs(retained: list[FAQDTO]) -> str:
        if not retained:
            return core_text
        faq_lines = ["Frequently Asked Questions:"]
        for f in retained:
            faq_lines.append(f"  - Q: {f.question}\n    A: {f.answer}")
        return core_text + "\n\n" + "\n".join(faq_lines)

    retained_faqs: list[FAQDTO] = []
    shed_count = 0

    for f in sorted_faqs:
        candidate_faqs = retained_faqs + [f]
        candidate_text = _render_context_with_faqs(candidate_faqs)
        if len(candidate_text) <= max_chars:
            retained_faqs.append(f)
        else:
            shed_count += 1
            logger.warning(
                "Shedding FAQ ID %d (topic='%s', sort_order=%d) to keep context within %d chars.",
                f.id,
                f.topic,
                f.sort_order,
                max_chars,
            )

    if shed_count > 0:
        logger.warning(
            "Clinic context exceeded %d chars. Deterministically shed %d FAQ(s). "
            "Retained %d FAQ(s).",
            max_chars,
            shed_count,
            len(retained_faqs),
        )

    return _render_context_with_faqs(retained_faqs)


async def load_clinic_context(
    session: AsyncSession,
    slug: str = "brightsmile",
    now: datetime | None = None,
) -> ClinicContextDTO:
    """Load clinic data via sequential async queries and assemble an immutable ClinicContextDTO.

    Args:
        session: Active SQLAlchemy async session.
        slug: Clinic unique slug identifier.
        now: Reference instant (must be timezone-aware if provided).
            Defaults to current UTC instant.

    Returns:
        Immutable `ClinicContextDTO` snapshot.

    Raises:
        ValueError: If `now` is timezone-naive.
        ClinicNotFoundError: If no BusinessProfile matches the requested slug.
    """
    if now is not None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("`now` must be an explicit, timezone-aware datetime instance.")
        utc_now = now
    else:
        utc_now = datetime.now(UTC)

    # 1. Fetch Profile by slug
    profile = await get_profile_by_slug(session, slug=slug)
    if profile is None:
        raise ClinicNotFoundError(f"Clinic profile with slug '{slug}' not found.")

    clinic_id = profile.id
    clinic_tz = resolve_clinic_timezone(profile.timezone)
    local_now = utc_now.astimezone(clinic_tz)

    # 2. Define closure date window: today through today + 30 days
    today_local = local_now.date()
    until_local = today_local + timedelta(days=30)

    # 3. Fetch all child entities using resolved clinic_id
    hours = await get_hours_by_clinic_id(session, clinic_id)
    closures = await get_upcoming_closures_by_clinic_id(
        session, clinic_id, today_local, until_local
    )
    services = await get_active_services_by_clinic_id(session, clinic_id)
    policies = await get_policies_by_clinic_id(session, clinic_id)
    faqs = await get_active_faqs_by_clinic_id(session, clinic_id)

    return ClinicContextDTO(
        profile=profile,
        hours=tuple(hours),
        closures=tuple(closures),
        services=tuple(services),
        policies=tuple(policies),
        faqs=tuple(faqs),
        local_now=local_now,
    )
