"""Phase 4 booking engine schema.

Revision ID: c4d5e6f7a8b9
Revises: b1c2d3e4f5a6
Create Date: 2026-11-16 01:10:00.000000

Creates the Phase 4 booking engine tables:
- appointments: confirmed and cancelled appointments with snapshotted configuration
- booking_proposals: server-side, expiring booking proposals with configuration snapshots
- consumed_consent_nonces: global anti-replay registry for consumed consent nonces
- caller_verification_sessions: PostgreSQL-persisted caller verification session state
- consent_challenges: PostgreSQL-persisted consent challenge lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. appointments table
    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("service_name_snapshot", sa.String(length=150), nullable=False),
        sa.Column("caller_name", sa.String(length=200), nullable=False),
        sa.Column("caller_phone", sa.String(length=30), nullable=False),
        sa.Column("caller_email", sa.String(length=200), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False),
        sa.Column("blocked_until_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="confirmed"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cancellation_reason", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_appointments_clinic_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name="fk_appointments_service_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("duration_minutes > 0", name="chk_appointment_duration_positive"),
        sa.CheckConstraint("buffer_minutes >= 0", name="chk_appointment_buffer_non_negative"),
        sa.CheckConstraint(
            "end_at = start_at + (duration_minutes * INTERVAL '1 minute')",
            name="chk_appointment_time_range",
        ),
        sa.CheckConstraint(
            "blocked_until_at = end_at + (buffer_minutes * INTERVAL '1 minute')",
            name="chk_appointment_blocked_until",
        ),
        sa.CheckConstraint("version >= 1", name="chk_appointment_version"),
        sa.CheckConstraint("status IN ('confirmed', 'cancelled')", name="chk_appointment_status"),
        sa.CheckConstraint(
            "char_length(trim(caller_name)) > 0", name="chk_appointment_caller_name"
        ),
        sa.CheckConstraint(
            "char_length(trim(caller_phone)) > 0", name="chk_appointment_caller_phone"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_appointments"),
    )
    op.create_index(
        "ix_appointments_clinic_range",
        "appointments",
        ["clinic_id", "start_at", "blocked_until_at"],
    )
    op.create_index(
        "ix_appointments_caller_lookup",
        "appointments",
        ["clinic_id", "caller_phone", "status"],
    )

    # 2. booking_proposals table
    op.create_table(
        "booking_proposals",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("service_name_snapshot", sa.String(length=150), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("confirmed_appointment_id", sa.Integer(), nullable=True),
        sa.Column("expected_appointment_version", sa.Integer(), nullable=True),
        sa.Column(
            "proposal_type", sa.String(length=50), nullable=False, server_default="new_booking"
        ),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("created_turn_id", sa.Integer(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False),
        sa.Column("caller_name", sa.String(length=200), nullable=False),
        sa.Column("caller_phone", sa.String(length=30), nullable=False),
        sa.Column("caller_email", sa.String(length=200), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_booking_proposals_clinic_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name="fk_booking_proposals_service_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["appointment_id"],
            ["appointments.id"],
            name="fk_booking_proposals_appointment_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_appointment_id"],
            ["appointments.id"],
            name="fk_booking_proposals_confirmed_appointment_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_booking_proposals"),
        sa.UniqueConstraint("idempotency_key", name="uq_booking_proposals_idempotency_key"),
        sa.CheckConstraint("duration_minutes > 0", name="chk_proposal_duration_positive"),
        sa.CheckConstraint("buffer_minutes >= 0", name="chk_proposal_buffer_non_negative"),
        sa.CheckConstraint(
            "end_at = start_at + (duration_minutes * INTERVAL '1 minute')",
            name="chk_proposal_time_range",
        ),
        sa.CheckConstraint(
            "blocked_until_at = end_at + (buffer_minutes * INTERVAL '1 minute')",
            name="chk_proposal_blocked_until",
        ),
        sa.CheckConstraint(
            "proposal_type IN ('new_booking', 'reschedule')",
            name="chk_proposal_type",
        ),
        sa.CheckConstraint(
            "("
            "(proposal_type = 'new_booking' AND appointment_id IS NULL AND expected_appointment_version IS NULL) "
            "OR "
            "(proposal_type = 'reschedule' AND appointment_id IS NOT NULL AND expected_appointment_version IS NOT NULL)"
            ")",
            name="chk_proposal_type_consistency",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'confirmed', 'expired', 'rejected')",
            name="chk_proposal_state",
        ),
        sa.CheckConstraint(
            "("
            "(state = 'pending' AND confirmed_appointment_id IS NULL AND consumed_at IS NULL) "
            "OR "
            "(state = 'confirmed' AND confirmed_appointment_id IS NOT NULL AND consumed_at IS NOT NULL) "
            "OR "
            "(state IN ('expired', 'rejected') AND confirmed_appointment_id IS NULL AND consumed_at IS NOT NULL)"
            ")",
            name="chk_proposal_state_linkage",
        ),
        sa.CheckConstraint("expires_at > created_at", name="chk_proposal_expiry"),
        sa.CheckConstraint("created_turn_id > 0", name="chk_proposal_turn"),
    )
    op.create_index(
        "ix_proposals_session_id",
        "booking_proposals",
        ["session_id"],
    )
    op.create_index(
        "ix_proposals_expires_at",
        "booking_proposals",
        ["expires_at"],
    )

    # 3. consumed_consent_nonces table
    op.create_table(
        "consumed_consent_nonces",
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column(
            "consumed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("nonce", name="pk_consumed_consent_nonces"),
    )
    op.create_index(
        "ix_consumed_nonces_session",
        "consumed_consent_nonces",
        ["session_id"],
    )

    # 4. caller_verification_sessions table
    op.create_table(
        "caller_verification_sessions",
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("verification_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("locked_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_appointment_id", sa.Integer(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_caller_verification_clinic_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verified_appointment_id"],
            ["appointments.id"],
            name="fk_caller_verification_appointment_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", name="pk_caller_verification_sessions"),
    )
    op.create_index(
        "ix_caller_verification_session_apt",
        "caller_verification_sessions",
        ["session_id", "verified_appointment_id"],
    )

    # 5. consent_challenges table
    op.create_table(
        "consent_challenges",
        sa.Column("challenge_id", UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=True),
        sa.Column("readback_details_hash", sa.String(length=64), nullable=False),
        sa.Column("readback_completed_turn_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("consumed_turn_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["business_profile.id"],
            name="fk_consent_challenges_clinic_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'authorized', 'invalidated', 'consumed')",
            name="chk_consent_challenge_status",
        ),
        sa.CheckConstraint("expires_at > created_at", name="chk_consent_challenge_expiry"),
        sa.PrimaryKeyConstraint("challenge_id", name="pk_consent_challenges"),
    )
    op.create_index(
        "ix_consent_challenges_target",
        "consent_challenges",
        ["target_id", "status"],
    )
    op.create_index(
        "uq_consent_challenge_active_session",
        "consent_challenges",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("consent_challenges")
    op.drop_table("caller_verification_sessions")
    op.drop_table("consumed_consent_nonces")
    op.drop_table("booking_proposals")
    op.drop_table("appointments")
