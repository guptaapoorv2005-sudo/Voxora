"""Runtime clinic date, time, and timezone context builder for Voxora.

In a Node.js/Express backend, you might inject the current server time via
`Intl.DateTimeFormat(..., { timeZone: "Asia/Kolkata" })` into an LLM system prompt.
In Python, we use the standard library `datetime` and `zoneinfo` modules so the
voice receptionist always knows what day and time it currently is at the clinic
without hardcoding dates or adding external date libraries.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("voxora.voice.context")

# India Standard Time (Asia/Kolkata) has no Daylight Saving Time transitions and
# stays fixed at UTC+05:30. This fallback is only used when running on Windows
# without the optional `tzdata` package installed; Linux/CI always uses ZoneInfo.
_IST_FALLBACK = timezone(timedelta(hours=5, minutes=30), name="IST")


def resolve_clinic_timezone(timezone_name: str) -> tzinfo:
    """Resolve an IANA timezone name (such as ``Asia/Kolkata``) to a ``tzinfo`` object.

    Args:
        timezone_name: Standard IANA timezone identifier (e.g. ``"Asia/Kolkata"``).

    Returns:
        A ``tzinfo`` instance suitable for converting UTC instants into clinic local time.

    Raises:
        ValueError: If ``timezone_name`` is blank or cannot be resolved.
    """
    cleaned = timezone_name.strip()
    if not cleaned:
        raise ValueError("clinic_timezone cannot be empty or whitespace.")

    try:
        return ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as exc:
        if cleaned == "Asia/Kolkata":
            logger.debug(
                "System IANA tzdata not found for 'Asia/Kolkata'; using fixed IST (UTC+05:30)."
            )
            return _IST_FALLBACK
        raise ValueError(f"Unknown or unavailable IANA timezone: {cleaned!r}") from exc


def build_session_context(
    timezone_name: str = "Asia/Kolkata",
    now: datetime | None = None,
) -> str:
    """Build a short, spoken-friendly date/time orientation line for a voice session.

    Passing `now` explicitly lets unit tests verify formatting deterministically
    without relying on wall-clock time (similar to injecting a mock Date in Jest).

    Args:
        timezone_name: Clinic IANA timezone identifier. Defaults to ``"Asia/Kolkata"``.
        now: Optional timezone-aware ``datetime`` instant. Defaults to current UTC time.

    Returns:
        A single-line context string injected at the top of the agent instructions.

    Raises:
        ValueError: If ``now`` is timezone-naive (missing ``tzinfo``) or timezone is invalid.
    """
    clinic_tz = resolve_clinic_timezone(timezone_name)

    if now is None:
        reference_utc = datetime.now(UTC)
    else:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("`now` must be a timezone-aware datetime.")
        reference_utc = now

    local_dt = reference_utc.astimezone(clinic_tz)

    # Note: `%I` gives 01-12 on both Windows and Linux (`%-I` fails on Windows),
    # so we strip the leading zero in Python and read `local_dt.day` directly.
    time_12h = local_dt.strftime("%I:%M %p").lstrip("0")
    spoken_date = f"{local_dt.strftime('%A')}, {local_dt.day} {local_dt.strftime('%B %Y')}"
    tz_abbrev = local_dt.tzname() or "IST"

    return (
        f"Current clinic date and time ({timezone_name.strip()}): "
        f"{spoken_date}, {time_12h} {tz_abbrev}."
    )
