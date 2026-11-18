"""Unit tests for clinic date, time, and timezone context formatting."""

from datetime import UTC, datetime

import pytest

from app.voice.context import build_session_context, resolve_clinic_timezone


def test_build_session_context_formats_saturday_14_nov_2026_ist() -> None:
    # 2026-11-14 04:00 UTC == 2026-11-14 09:30 AM IST (Saturday).
    sample_utc = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
    result = build_session_context("Asia/Kolkata", now=sample_utc)

    assert "Asia/Kolkata" in result
    assert "Saturday, 14 November 2026" in result
    assert "9:30 AM" in result
    assert "IST" in result


def test_build_session_context_formats_pm_single_digit_day_and_hour() -> None:
    # 2026-11-05 13:05 UTC == 2026-11-05 6:35 PM IST (Thursday).
    sample_utc = datetime(2026, 11, 5, 13, 5, tzinfo=UTC)
    result = build_session_context("Asia/Kolkata", now=sample_utc)

    assert "Thursday, 5 November 2026, 6:35 PM IST" in result


def test_build_session_context_rejects_naive_datetime() -> None:
    naive = datetime(2026, 11, 12, 18, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_session_context("Asia/Kolkata", now=naive)


def test_resolve_clinic_timezone_rejects_blank_or_invalid() -> None:
    with pytest.raises(ValueError):
        resolve_clinic_timezone("   ")
    with pytest.raises(ValueError, match="Unknown or unavailable IANA timezone"):
        resolve_clinic_timezone("Mars/Olympus_Mons")
