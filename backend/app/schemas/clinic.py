"""Pydantic schemas for FastAPI read-only clinic endpoints.

These schemas act as the public API contract (like TypeScript interfaces in a Next.js app)
defining the exact JSON structures returned by `/api/clinic/...` endpoints.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ClinicProfileResponse(BaseModel):
    """Public clinic profile information."""

    model_config = ConfigDict(from_attributes=True)

    slug: str = Field(description="Unique clinic identifier")
    name: str = Field(description="Display name of the clinic")
    description: str = Field(description="Clinic description")
    address_line1: str
    address_line2: str | None = None
    city: str
    state: str
    postal_code: str
    country: str
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    timezone: str = Field(description="IANA timezone string")


class BusinessHoursResponse(BaseModel):
    """Operating hours for a specific day of the week."""

    model_config = ConfigDict(from_attributes=True)

    day_of_week: int = Field(ge=0, le=6, description="0=Monday, 6=Sunday")
    is_closed: bool
    open_time: time | None = None
    close_time: time | None = None
    break_start: time | None = None
    break_end: time | None = None


class BusinessClosureResponse(BaseModel):
    """Scheduled calendar closures and holidays."""

    model_config = ConfigDict(from_attributes=True)

    date: date
    reason: str
    all_day: bool = True


class ServiceResponse(BaseModel):
    """Available clinical dental service."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    duration_minutes: int
    buffer_minutes: int
    price_inr: Decimal | None = None
    min_lead_time_hours: int
    cancellation_window_hours: int
    sort_order: int


class ClinicPolicyResponse(BaseModel):
    """Spoken clinic policy."""

    model_config = ConfigDict(from_attributes=True)

    policy_type: str
    title: str
    content: str
    sort_order: int


class FAQResponse(BaseModel):
    """Frequently asked question and answer."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    topic: str
    question: str
    answer: str
    sort_order: int
