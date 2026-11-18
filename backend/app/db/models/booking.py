"""SQLAlchemy ORM models for Phase 4 Booking Engine and Phase 5 Voice Operations.

Defines persistence models for:
- Appointment: confirmed and cancelled appointments with snapshotted configuration
- BookingProposal: server-side, expiring booking proposals with configuration snapshots
- CancellationProposal: server-side cancellation proposals with controlled categories
- CallerVerificationSession: PostgreSQL-persisted caller verification session state
- ConsentChallenge: PostgreSQL-persisted consent challenge lifecycle
- ClientVerificationAttempt: sliding-window tracking for standard and step-up verification attempts
- AppointmentVerificationFailure: cross-client verification failure tracking for step-up triggers
- TokenIssuanceAttempt: keyed IP hash tracking for token issuance rate limiting
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Appointment(Base):
    """Canonical clinic appointment with immutable configuration snapshots and version tracking."""

    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    service_name_snapshot: Mapped[str] = mapped_column(
        String(150), nullable=False, doc="Immutable service name at booking time"
    )
    caller_name: Mapped[str] = mapped_column(String(200), nullable=False)
    caller_phone: Mapped[str] = mapped_column(String(30), nullable=False)
    caller_email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    blocked_until_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(50), nullable=False, default="confirmed")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cancellation_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    clinic = relationship("BusinessProfile")
    service = relationship("Service")

    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="chk_appointment_duration_positive"),
        CheckConstraint("buffer_minutes >= 0", name="chk_appointment_buffer_non_negative"),
        CheckConstraint(
            "end_at = start_at + (duration_minutes * INTERVAL '1 minute')",
            name="chk_appointment_time_range",
        ),
        CheckConstraint(
            "blocked_until_at = end_at + (buffer_minutes * INTERVAL '1 minute')",
            name="chk_appointment_blocked_until",
        ),
        CheckConstraint("version >= 1", name="chk_appointment_version"),
        CheckConstraint("status IN ('confirmed', 'cancelled')", name="chk_appointment_status"),
        CheckConstraint("char_length(trim(caller_name)) > 0", name="chk_appointment_caller_name"),
        CheckConstraint("char_length(trim(caller_phone)) > 0", name="chk_appointment_caller_phone"),
        Index("ix_appointments_clinic_range", "clinic_id", "start_at", "blocked_until_at"),
        Index("ix_appointments_caller_lookup", "clinic_id", "caller_phone", "status"),
    )


class ConsentChallenge(Base):
    """PostgreSQL-persisted consent challenge lifecycle ensuring cross-worker consistency."""

    __tablename__ = "consent_challenges"

    challenge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    readback_details_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    readback_completed_turn_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    readback_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending_readback")
    invalidation_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_turn_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consumed_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    clinic = relationship("BusinessProfile")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_readback', 'pending', 'invalidated', 'consumed')",
            name="chk_consent_challenge_status",
        ),
        CheckConstraint(
            "action_type IN ('create_booking', 'reschedule_appointment', 'cancel_appointment')",
            name="chk_consent_challenge_action_type",
        ),
        CheckConstraint("expires_at > created_at", name="chk_consent_challenge_expiry"),
        Index("ix_consent_challenges_target", "target_id", "status"),
        Index(
            "uq_consent_challenge_active_session",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('pending_readback', 'pending')"),
        ),
        Index(
            "uq_consent_challenge_authorized_turn",
            "session_id",
            "consumed_turn_id",
            unique=True,
            postgresql_where=text("consumed_turn_id IS NOT NULL AND status = 'consumed'"),
        ),
        Index(
            "uq_consent_challenge_consumed_item",
            "session_id",
            "consumed_item_id",
            unique=True,
            postgresql_where=text("consumed_item_id IS NOT NULL AND status = 'consumed'"),
        ),
    )


class BookingProposal(Base):
    """Expiring server-side booking proposal capturing an immutable offer snapshot."""

    __tablename__ = "booking_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    service_name_snapshot: Mapped[str] = mapped_column(
        String(150), nullable=False, doc="Immutable service name at proposal creation"
    )
    appointment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=True
    )
    confirmed_appointment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=True
    )
    expected_appointment_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    proposal_type: Mapped[str] = mapped_column(String(50), nullable=False, default="new_booking")
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_turn_id: Mapped[int] = mapped_column(Integer, nullable=False)

    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    caller_name: Mapped[str] = mapped_column(String(200), nullable=False)
    caller_phone: Mapped[str] = mapped_column(String(30), nullable=False)
    caller_email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    blocked_until_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    state: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    challenge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("consent_challenges.challenge_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    readback_details_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    clinic = relationship("BusinessProfile")
    service = relationship("Service")
    target_appointment = relationship("Appointment", foreign_keys=[appointment_id])
    confirmed_appointment = relationship("Appointment", foreign_keys=[confirmed_appointment_id])
    challenge = relationship("ConsentChallenge")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_booking_proposals_idempotency_key"),
        UniqueConstraint("challenge_id", name="uq_booking_proposals_challenge_id"),
        CheckConstraint("duration_minutes > 0", name="chk_proposal_duration_positive"),
        CheckConstraint("buffer_minutes >= 0", name="chk_proposal_buffer_non_negative"),
        CheckConstraint(
            "end_at = start_at + (duration_minutes * INTERVAL '1 minute')",
            name="chk_proposal_time_range",
        ),
        CheckConstraint(
            "blocked_until_at = end_at + (buffer_minutes * INTERVAL '1 minute')",
            name="chk_proposal_blocked_until",
        ),
        CheckConstraint("proposal_type IN ('new_booking', 'reschedule')", name="chk_proposal_type"),
        CheckConstraint(
            "("
            "(proposal_type = 'new_booking' AND appointment_id IS NULL AND expected_appointment_version IS NULL) "
            "OR "
            "(proposal_type = 'reschedule' AND appointment_id IS NOT NULL AND expected_appointment_version IS NOT NULL)"
            ")",
            name="chk_proposal_type_consistency",
        ),
        CheckConstraint(
            "state IN ('pending', 'confirmed', 'expired', 'rejected', 'replaced')",
            name="chk_proposal_state",
        ),
        CheckConstraint(
            "("
            "(state = 'pending' AND confirmed_appointment_id IS NULL AND consumed_at IS NULL) "
            "OR "
            "(state = 'confirmed' AND confirmed_appointment_id IS NOT NULL AND consumed_at IS NOT NULL) "
            "OR "
            "(state IN ('expired', 'rejected', 'replaced') AND confirmed_appointment_id IS NULL AND consumed_at IS NOT NULL)"
            ")",
            name="chk_proposal_state_linkage",
        ),
        CheckConstraint("expires_at > created_at", name="chk_proposal_expiry"),
        CheckConstraint("created_turn_id > 0", name="chk_proposal_turn"),
        Index("ix_proposals_session_id", "session_id"),
        Index("ix_proposals_expires_at", "expires_at"),
        Index(
            "uq_booking_proposals_active_session",
            "session_id",
            unique=True,
            postgresql_where=text("state = 'pending' AND proposal_type = 'new_booking'"),
        ),
        Index(
            "uq_reschedule_proposals_active_apt",
            "appointment_id",
            unique=True,
            postgresql_where=text(
                "state = 'pending' AND proposal_type = 'reschedule' AND appointment_id IS NOT NULL"
            ),
        ),
    )


class CancellationProposal(Base):
    """Server-side proposal capturing an immutable cancellation request with controlled reason categories."""

    __tablename__ = "cancellation_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False
    )
    appointment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    expected_appointment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    caller_name: Mapped[str] = mapped_column(String(200), nullable=False)
    caller_phone: Mapped[str] = mapped_column(String(30), nullable=False)
    reason_category: Mapped[str] = mapped_column(String(50), nullable=False)
    state: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    challenge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("consent_challenges.challenge_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    readback_details_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    clinic = relationship("BusinessProfile")
    appointment = relationship("Appointment")
    challenge = relationship("ConsentChallenge")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_cancellation_proposals_idempotency_key"),
        UniqueConstraint("challenge_id", name="uq_cancellation_proposals_challenge_id"),
        CheckConstraint(
            "state IN ('pending', 'confirmed', 'expired', 'replaced')",
            name="chk_cancellation_proposals_state",
        ),
        CheckConstraint(
            "reason_category IN ('scheduling_conflict', 'patient_request', 'provider_unavailable', 'no_show', 'other')",
            name="chk_cancellation_proposals_reason_category",
        ),
        CheckConstraint("created_turn_id > 0", name="chk_cancellation_proposal_turn"),
        CheckConstraint("expires_at > created_at", name="chk_cancellation_proposal_expiry"),
        Index("ix_cancellation_proposals_session", "session_id"),
        Index("ix_cancellation_proposals_expires", "expires_at"),
        Index(
            "uq_cancellation_proposals_active_apt",
            "appointment_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
        Index(
            "uq_cancellation_proposals_confirmed_apt",
            "appointment_id",
            unique=True,
            postgresql_where=text("state = 'confirmed'"),
        ),
    )


class CallerVerificationSession(Base):
    """PostgreSQL-persisted verification session state tracking attempts and verified appointment."""

    __tablename__ = "caller_verification_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False
    )
    verification_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_appointment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    handed_off: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    handed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handoff_category: Mapped[str | None] = mapped_column(String(50), nullable=True)

    clinic = relationship("BusinessProfile")
    verified_appointment = relationship("Appointment")

    __table_args__ = (
        Index("ix_caller_verification_session_apt", "session_id", "verified_appointment_id"),
    )


class ClientVerificationAttempt(Base):
    """Sliding-window rate-limiting tracking for client verification attempts."""

    __tablename__ = "client_verification_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="standard")
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("attempt_type IN ('standard', 'step_up')", name="chk_client_attempt_type"),
        Index("ix_client_attempts_window", "client_id", "attempt_type", "attempted_at"),
    )


class AppointmentVerificationFailure(Base):
    """Tracking for appointment-specific verification failures triggering step-up mode."""

    __tablename__ = "appointment_verification_failures"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_apt_failures_window", "appointment_id", "attempted_at"),
        Index("ix_apt_failures_client", "appointment_id", "client_id"),
    )


class TokenIssuanceAttempt(Base):
    """Keyed IP hash tracking for token issuance rate limiting."""

    __tablename__ = "token_issuance_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_ip_hash: Mapped[str] = mapped_column(String(16), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_token_attempts_window", "client_ip_hash", "attempted_at"),)
