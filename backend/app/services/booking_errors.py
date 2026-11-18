"""Typed domain exceptions for the Phase 4 booking engine.

Every exception inherits from `BookingDomainError` to allow caller layers to catch
domain-specific failures cleanly without catching generic Python runtime errors.
"""

from __future__ import annotations


class BookingDomainError(Exception):
    """Base exception for all booking and availability domain errors."""

    def __init__(self, message: str = "A booking domain error occurred."):
        super().__init__(message)
        self.message = message


class ProposalNotFoundError(BookingDomainError):
    """Raised when a booking proposal ID does not exist in the database."""


class ProposalExpiredError(BookingDomainError):
    """Raised when an operation is attempted on an expired booking proposal."""


class ProposalAlreadyUsedError(BookingDomainError):
    """Raised when a proposal has already been confirmed, rejected, or consumed."""


class SlotUnavailableError(BookingDomainError):
    """Raised when a requested slot is unavailable due to an existing booking or closure."""


class InvalidConfirmationTurnError(BookingDomainError):
    """Raised when confirmation turn sequence is invalid (e.g. not strictly greater than creation/readback turn)."""


class InvalidConsentError(BookingDomainError):
    """Raised when consent authorization is invalid, tampered, forged, or mismatched."""


class ExpiredConsentError(BookingDomainError):
    """Raised when a consent authorization token or challenge has expired."""


class SessionMismatchError(BookingDomainError):
    """Raised when an operation is attempted with a voice session ID differing from the proposal's session ID."""


class AppointmentNotFoundError(BookingDomainError):
    """Raised when an appointment ID does not exist in the database."""


class InvalidAppointmentStatusError(BookingDomainError):
    """Raised when rescheduling or modifying an appointment that is not confirmed."""


class StaleAppointmentVersionError(BookingDomainError):
    """Raised when modifying an appointment whose version changed after consent/proposal creation."""


class VerificationRequiredError(BookingDomainError):
    """Raised when attempting appointment access, reschedule, or cancellation without caller verification."""


class VerificationFailedError(BookingDomainError):
    """Raised when caller verification credentials do not match any confirmed appointment."""


class VerificationExpiredError(BookingDomainError):
    """Raised when caller verification status has expired relative to its TTL."""


class CancellationWindowError(BookingDomainError):
    """Raised when an appointment cancellation is attempted within the service's cancellation window."""


class IdempotencyConflictError(BookingDomainError):
    """Raised when an idempotency key matches an existing proposal with differing immutable parameters."""


class IdempotencyRaceError(BookingDomainError):
    """Raised when a proposal race collision occurs but the winning record cannot be resolved."""


class InconsistentProposalStateError(BookingDomainError):
    """Raised when a proposal's database state contradicts its linkage (e.g. confirmed with null link)."""


class InvalidInputError(BookingDomainError):
    """Raised when caller input parameters (e.g. phone, last4) fail strict validation."""


class InvalidTimezoneError(BookingDomainError):
    """Raised when a local wall-clock time does not exist due to DST transition."""


class AmbiguousTimezoneError(BookingDomainError):
    """Raised when a local wall-clock time is ambiguous during DST fall-back."""
