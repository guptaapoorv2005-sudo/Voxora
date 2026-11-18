"""Unit tests for Phase 4 booking domain exceptions."""

from __future__ import annotations

import pytest

from app.services.booking_errors import (
    AmbiguousTimezoneError,
    AppointmentNotFoundError,
    BookingDomainError,
    CancellationWindowError,
    ExpiredConsentError,
    IdempotencyConflictError,
    IdempotencyRaceError,
    InconsistentProposalStateError,
    InvalidAppointmentStatusError,
    InvalidConfirmationTurnError,
    InvalidConsentError,
    InvalidInputError,
    InvalidTimezoneError,
    ProposalAlreadyUsedError,
    ProposalExpiredError,
    ProposalNotFoundError,
    SlotUnavailableError,
    StaleAppointmentVersionError,
    VerificationExpiredError,
    VerificationFailedError,
    VerificationRequiredError,
)

ALL_EXCEPTIONS = [
    ProposalNotFoundError,
    ProposalExpiredError,
    ProposalAlreadyUsedError,
    SlotUnavailableError,
    InvalidConfirmationTurnError,
    InvalidConsentError,
    ExpiredConsentError,
    AppointmentNotFoundError,
    InvalidAppointmentStatusError,
    StaleAppointmentVersionError,
    VerificationRequiredError,
    VerificationFailedError,
    VerificationExpiredError,
    CancellationWindowError,
    IdempotencyConflictError,
    IdempotencyRaceError,
    InconsistentProposalStateError,
    InvalidInputError,
    InvalidTimezoneError,
    AmbiguousTimezoneError,
]


@pytest.mark.parametrize("exc_cls", ALL_EXCEPTIONS)
def test_exceptions_inherit_from_booking_domain_error(exc_cls):
    """Verify that every domain exception inherits from BookingDomainError."""
    assert issubclass(exc_cls, BookingDomainError)
    assert issubclass(exc_cls, Exception)


@pytest.mark.parametrize("exc_cls", ALL_EXCEPTIONS)
def test_exceptions_preserve_custom_message(exc_cls):
    """Verify that custom messages are preserved in str() and .message attribute."""
    msg = f"Custom failure message for {exc_cls.__name__}"
    err = exc_cls(msg)
    assert str(err) == msg
    assert err.message == msg
