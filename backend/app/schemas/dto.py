"""Immutable Data Transfer Objects (DTOs) for clinic and booking domain entities.

Why use frozen dataclass DTOs instead of passing SQLAlchemy ORM objects?
In Node.js/MERN, you might pass plain JSON documents between layers. In Python with SQLAlchemy,
ORM model instances are bound to an active database session (`AsyncSession`).
Passing live ORM instances into prompt builders or API layers risks detached-instance errors,
accidental lazy loading over the network, or unexpected mutations.

Frozen dataclasses act as immutable, session-independent snapshots of clinic state.
Once loaded from PostgreSQL, these DTOs can be safely manipulated, tested, and passed
to prompt builders or unit tests without requiring an open database transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True)
class ClinicProfileDTO:
    """Immutable representation of the clinic identity and regional settings."""

    id: int
    slug: str
    name: str
    description: str
    address_line1: str
    address_line2: str | None
    city: str
    state: str
    postal_code: str
    country: str
    phone: str | None
    email: str | None
    website: str | None
    timezone: str


@dataclass(frozen=True)
class BusinessHoursDTO:
    """Operating hours for a specific day of the week (0=Monday through 6=Sunday)."""

    day_of_week: int
    is_closed: bool
    open_time: time | None
    close_time: time | None
    break_start: time | None
    break_end: time | None


@dataclass(frozen=True)
class BusinessClosureDTO:
    """An all-day closure date and reason (e.g., national holiday)."""

    date: date
    reason: str
    all_day: bool = True


@dataclass(frozen=True)
class ServiceDTO:
    """Offered clinical service with pricing and timing constraints."""

    id: int
    name: str
    description: str | None
    duration_minutes: int
    buffer_minutes: int
    price_inr: Decimal | None
    min_lead_time_hours: int
    cancellation_window_hours: int
    sort_order: int


@dataclass(frozen=True)
class ClinicPolicyDTO:
    """Spoken policy text explaining cancellation or clinic operations."""

    policy_type: str
    title: str
    content: str
    sort_order: int


@dataclass(frozen=True)
class FAQDTO:
    """Question and answer pair grounded in canonical clinic documentation."""

    id: int
    topic: str
    question: str
    answer: str
    sort_order: int


@dataclass(frozen=True)
class ClinicContextDTO:
    """Complete aggregated snapshot of clinic data ready for prompt building.

    All collection attributes are frozen tuples to ensure genuine immutability
    across repository, service, and voice worker layers.
    """

    profile: ClinicProfileDTO
    hours: tuple[BusinessHoursDTO, ...]
    closures: tuple[BusinessClosureDTO, ...]
    services: tuple[ServiceDTO, ...]
    policies: tuple[ClinicPolicyDTO, ...]
    faqs: tuple[FAQDTO, ...]
    local_now: datetime


# =============================================================================
# Phase 4 Booking Engine DTOs
# =============================================================================


@dataclass(frozen=True)
class SlotDTO:
    """An available time slot calculated for a specific clinic and service."""

    start_at: datetime
    end_at: datetime
    duration_minutes: int
    buffer_minutes: int
    blocked_until_at: datetime


@dataclass(frozen=True)
class AppointmentDTO:
    """Immutable representation of a confirmed or cancelled clinic appointment."""

    id: int
    clinic_id: int
    service_id: int
    service_name: str
    caller_name: str
    caller_phone: str
    caller_email: str | None
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    buffer_minutes: int
    blocked_until_at: datetime
    status: str
    version: int
    cancellation_reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ProposalDTO:
    """Immutable representation of an expiring server-side booking proposal."""

    id: UUID
    clinic_id: int
    service_id: int
    service_name: str
    appointment_id: int | None
    confirmed_appointment_id: int | None
    expected_appointment_version: int | None
    proposal_type: str
    session_id: str
    created_turn_id: int
    duration_minutes: int
    buffer_minutes: int
    caller_name: str
    caller_phone: str
    caller_email: str | None
    start_at: datetime
    end_at: datetime
    blocked_until_at: datetime
    state: str
    expires_at: datetime
    idempotency_key: str
    created_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class ConsentAuthorization:
    """Cryptographically signed, turn-bound, single-use affirmative authorization token."""

    session_id: str
    action_type: str
    target_id: str
    authorized_turn_id: int
    challenge_id: str
    nonce: str
    created_at: datetime
    expires_at: datetime
    ttl_seconds: int
    signature: str
    expected_target_version: int | None = None


@dataclass(frozen=True)
class VerificationResult:
    """Result of caller identity verification against existing appointments."""

    success: bool
    appointment: AppointmentDTO | None = None
    handoff_required: bool = False
    advisory_message: str | None = None


@dataclass(frozen=True)
class ConsentChallengeDTO:
    """Snapshot of an active or consumed consent challenge."""

    challenge_id: UUID
    session_id: str
    clinic_id: int
    action_type: str
    target_id: str
    target_version: int | None
    readback_details_hash: str
    readback_completed_turn_id: int
    status: str
    consumed_turn_id: int | None
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
