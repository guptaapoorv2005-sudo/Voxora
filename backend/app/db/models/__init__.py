"""ORM models package for Voxora.

Re-exports `Base` alongside all registered domain models so Alembic's `target_metadata`
discovers the complete schema during autogeneration, while preserving all existing
imports of `Base` across the codebase.
"""

from app.db.models.booking import (
    Appointment,
    AppointmentVerificationFailure,
    BookingProposal,
    CallerVerificationSession,
    CancellationProposal,
    ClientVerificationAttempt,
    ConsentChallenge,
    TokenIssuanceAttempt,
)
from app.db.models.call import Call, CallTraceEvent
from app.db.models.google_calendar import GoogleCalendarConnection, GoogleCalendarEvent
from app.db.models.clinic import (
    FAQ,
    BusinessClosure,
    BusinessHours,
    BusinessProfile,
    ClinicPolicy,
    Service,
)
from app.db.models.owner import OwnerLoginAttempt, OwnerSession, OwnerUser
from app.db.session import Base

__all__ = [
    "Appointment",
    "AppointmentVerificationFailure",
    "Base",
    "BookingProposal",
    "BusinessClosure",
    "BusinessHours",
    "BusinessProfile",
    "CallerVerificationSession",
    "Call",
    "CallTraceEvent",
    "CancellationProposal",
    "ClientVerificationAttempt",
    "ClinicPolicy",
    "ConsentChallenge",
    "FAQ",
    "GoogleCalendarConnection",
    "GoogleCalendarEvent",
    "OwnerUser",
    "OwnerSession",
    "OwnerLoginAttempt",
    "Service",
    "TokenIssuanceAttempt",
]

