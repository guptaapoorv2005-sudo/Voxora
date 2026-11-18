"""Current clinic facts for the voice interface, with bounded tool responses."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.clinic_repo import (
    get_active_faqs_by_clinic_id,
    get_active_services_by_clinic_id,
    get_hours_by_clinic_id,
    get_policies_by_clinic_id,
    get_profile_by_id,
    get_upcoming_closures_by_clinic_id,
)
from app.voice.context import resolve_clinic_timezone

MAX_FACT_CHARS = 2000
MAX_SERVICES = 30
UNKNOWN = "I do not have that information. Please contact the clinic reception."


def _words(value: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", value.lower()))
    return {
        word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word
        for word in words
    }


def _bounded(text: str) -> str:
    if len(text) > MAX_FACT_CHARS:
        return "The requested information is too long to provide safely. Please contact reception."
    return text


async def lookup_clinic_info(
    session: AsyncSession, clinic_id: int, topic: str, on_date: str | None = None
) -> str:
    """Return a matching configured fact; an unmatched or ambiguous request fails closed."""
    topic = topic.strip()
    if not topic or len(topic) > 500:
        return "Please provide a short clinic information topic."
    requested_day = None
    if on_date is not None:
        try:
            requested_day = date.fromisoformat(on_date.strip())
        except ValueError:
            return "Please provide the date in YYYY-MM-DD format."
    profile = await get_profile_by_id(session, clinic_id)
    if profile is None:
        return "Clinic information is temporarily unavailable. Please contact reception."
    words = _words(topic)
    normalized = topic.casefold()

    location_question = "where" in words and words <= {
        "where",
        "is",
        "the",
        "clinic",
        "located",
        "you",
        "are",
        "your",
        "office",
    }
    if words & {"address", "location"} or location_question:
        address = ", ".join(
            filter(
                None,
                (
                    profile.address_line1,
                    profile.address_line2,
                    profile.city,
                    profile.state,
                    profile.postal_code,
                    profile.country,
                ),
            )
        )
        return _bounded(f"The clinic is located at {address}.")
    if words & {"phone", "contact", "telephone", "email", "website"}:
        if words & {"phone", "telephone"} and not profile.phone:
            return "No phone number is listed. Please contact the clinic reception."
        if "email" in words and not profile.email:
            return "No email address is listed. Please contact the clinic reception."
        if "website" in words and not profile.website:
            return "No website is listed. Please contact the clinic reception."
        details = []
        if profile.phone:
            details.append(f"phone: {profile.phone}")
        if profile.email:
            details.append(f"email: {profile.email}")
        if profile.website:
            details.append(f"website: {profile.website}")
        return (
            _bounded("Clinic contact: " + "; ".join(details) + ".")
            if details
            else ("No phone number, email address, or website is listed. Please contact reception.")
        )

    if words & {"name", "description", "about"}:
        return _bounded(f"Clinic name: {profile.name}. Description: {profile.description}")

    policies = await get_policies_by_clinic_id(session, clinic_id)
    matches = [
        p
        for p in policies
        if words & _words(p.policy_type.replace("_", " ")) or normalized == p.title.casefold()
    ]
    if not matches and words & {"policy", "policie"}:
        matches = list(policies)
    if matches:
        return _bounded("Clinic policies: " + " ".join(f"{p.title}: {p.content}" for p in matches))

    if words & {"hour", "open", "opening", "time", "closed", "closure", "holiday"}:
        tz = resolve_clinic_timezone(profile.timezone)
        local_day = requested_day or datetime.now(UTC).astimezone(tz).date()
        if requested_day is None and words & {"closure", "holiday"}:
            end_day = local_day + timedelta(days=30)
            closures = await get_upcoming_closures_by_clinic_id(
                session, clinic_id, local_day, end_day
            )
            if not closures:
                return f"No closures are listed from {local_day.isoformat()} through {end_day.isoformat()}."
            return _bounded(
                f"Closures from {local_day.isoformat()} through {end_day.isoformat()}: "
                + "; ".join(f"{c.date.isoformat()}: {c.reason}" for c in closures)
            )
        hours = await get_hours_by_clinic_id(session, clinic_id)
        closures = await get_upcoming_closures_by_clinic_id(
            session, clinic_id, local_day, local_day
        )
        days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        entry = next((h for h in hours if h.day_of_week == local_day.weekday()), None)
        if closures:
            status = f"Closed on {local_day.isoformat()}: {closures[0].reason}."
        elif entry is None:
            status = f"Hours for {local_day.isoformat()} are not configured."
        elif entry.is_closed or entry.open_time is None or entry.close_time is None:
            status = f"Closed on {local_day.isoformat()}."
        else:
            status = (
                f"On {local_day.isoformat()}, open {entry.open_time.strftime('%I:%M %p')} "
                f"to {entry.close_time.strftime('%I:%M %p')}"
            )
            if entry.break_start and entry.break_end:
                status += f", break {entry.break_start.strftime('%I:%M %p')} to {entry.break_end.strftime('%I:%M %p')}"
            status += "."
        parts = []
        for h in hours:
            if h.is_closed or h.open_time is None or h.close_time is None:
                parts.append(f"{days[h.day_of_week]}: closed")
            else:
                line = f"{days[h.day_of_week]}: {h.open_time.strftime('%I:%M %p')} to {h.close_time.strftime('%I:%M %p')}"
                if h.break_start and h.break_end:
                    line += f", break {h.break_start.strftime('%I:%M %p')} to {h.break_end.strftime('%I:%M %p')}"
                parts.append(line)
        regular = " Regular opening hours: " + "; ".join(parts) + "." if parts else ""
        return _bounded(status + regular)

    faqs = await get_active_faqs_by_clinic_id(session, clinic_id)
    exact = [
        f for f in faqs if normalized in {f.topic.strip().casefold(), f.question.strip().casefold()}
    ]
    candidates = exact or [f for f in faqs if _words(f.topic) and _words(f.topic) <= words]
    if len(candidates) == 1:
        return _bounded(candidates[0].answer)
    if len(candidates) > 1:
        return "Please clarify which clinic question you mean."
    return UNKNOWN


async def lookup_clinic_services(
    session: AsyncSession, clinic_id: int, service_name: str | None = None
) -> str:
    """Return active services with price and duration, bounded without hiding omissions."""
    services = await get_active_services_by_clinic_id(session, clinic_id)
    if not services:
        return "No services are currently listed."
    if service_name is not None:
        name = service_name.strip().casefold()
        if not name or len(name) > 200:
            return "Please provide a valid service name."
        matches = [service for service in services if service.name.casefold() == name]
        if not matches:
            matches = [service for service in services if name in service.name.casefold()]
        if len(matches) > 1:
            return "Please clarify which service you mean."
        if not matches:
            return "That active service is not listed. Please contact reception."
        service = matches[0]
        price = (
            f"INR {service.price_inr:.2f}" if service.price_inr is not None else "price not listed"
        )
        description = service.description or "No description is listed."
        return _bounded(
            f"{service.name} (ID: {service.id}): {service.duration_minutes} minutes, {price}. "
            f"Description: {description}"
        )
    lines: list[str] = []
    for service in services[:MAX_SERVICES]:
        price = (
            f"INR {service.price_inr:.2f}" if service.price_inr is not None else "price not listed"
        )
        line = f"{service.name} (ID: {service.id}): {service.duration_minutes} minutes, {price}"
        if len("Available services: " + "; ".join([*lines, line])) > MAX_FACT_CHARS - 100:
            break
        lines.append(line)
    if not lines:
        return "The service list is too long to provide safely. Please contact reception."
    suffix = (
        " Additional services are not shown; contact reception for the complete list."
        if len(lines) < len(services)
        else "."
    )
    return "Available services: " + "; ".join(lines) + suffix
