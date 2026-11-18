"""phase5_readback_and_lockout

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Update caller_verification_sessions
    # -------------------------------------------------------------------------
    op.add_column(
        "caller_verification_sessions",
        sa.Column("handed_off", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "caller_verification_sessions",
        sa.Column("handed_off_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "caller_verification_sessions",
        sa.Column("handoff_category", sa.String(50), nullable=True),
    )

    # -------------------------------------------------------------------------
    # 2. Update consent_challenges columns & nullability
    # -------------------------------------------------------------------------
    op.add_column(
        "consent_challenges",
        sa.Column("authorization_nonce", sa.String(32), nullable=True),
    )
    op.add_column(
        "consent_challenges",
        sa.Column("authorization_signature", sa.String(64), nullable=True),
    )
    op.add_column(
        "consent_challenges",
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "consent_challenges",
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "consent_challenges",
        sa.Column("invalidation_reason", sa.String(50), nullable=True),
    )
    op.add_column(
        "consent_challenges",
        sa.Column("readback_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "consent_challenges",
        sa.Column("consumed_item_id", sa.String(64), nullable=True),
    )
    op.alter_column(
        "consent_challenges",
        "readback_completed_turn_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    # -------------------------------------------------------------------------
    # 3. Update consent_challenges check constraints
    # -------------------------------------------------------------------------
    op.drop_constraint("chk_consent_challenge_status", "consent_challenges", type_="check")
    op.create_check_constraint(
        "chk_consent_challenge_status",
        "consent_challenges",
        "status IN ('pending_readback', 'pending', 'authorized', 'invalidated', 'consumed')",
    )
    op.create_check_constraint(
        "chk_consent_challenge_action_type",
        "consent_challenges",
        "action_type IN ('create_booking', 'reschedule_appointment', 'cancel_appointment')",
    )

    # -------------------------------------------------------------------------
    # 4. Invalidate duplicate historical active challenges before partial indexes
    # -------------------------------------------------------------------------
    op.execute("""
        UPDATE consent_challenges
        SET status = 'invalidated', invalidated_at = NOW(), invalidation_reason = 'migration_deduplication'
        WHERE status IN ('pending_readback', 'pending', 'authorized')
          AND challenge_id IN (
              SELECT challenge_id FROM (
                  SELECT challenge_id,
                         ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY created_at DESC, challenge_id DESC) as rn
                  FROM consent_challenges
                  WHERE status IN ('pending_readback', 'pending', 'authorized')
              ) ranked
              WHERE ranked.rn > 1
          );
    """)

    # -------------------------------------------------------------------------
    # 5. Expand booking_proposals state constraints to include 'replaced'
    # -------------------------------------------------------------------------
    op.drop_constraint("chk_proposal_state_linkage", "booking_proposals", type_="check")
    op.drop_constraint("chk_proposal_state", "booking_proposals", type_="check")
    op.create_check_constraint(
        "chk_proposal_state",
        "booking_proposals",
        "state IN ('pending', 'confirmed', 'expired', 'rejected', 'replaced')",
    )
    op.create_check_constraint(
        "chk_proposal_state_linkage",
        "booking_proposals",
        "("
        "(state = 'pending' AND confirmed_appointment_id IS NULL AND consumed_at IS NULL) "
        "OR "
        "(state = 'confirmed' AND confirmed_appointment_id IS NOT NULL AND consumed_at IS NOT NULL) "
        "OR "
        "(state IN ('expired', 'rejected', 'replaced') AND confirmed_appointment_id IS NULL AND consumed_at IS NOT NULL)"
        ")",
    )

    # -------------------------------------------------------------------------
    # 6. Add columns to booking_proposals, backfill, then apply constraints
    # -------------------------------------------------------------------------
    op.add_column(
        "booking_proposals",
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "booking_proposals",
        sa.Column("readback_details_hash", sa.String(64), nullable=True),
    )

    # Safe backfill for existing Phase 4 booking_proposals rows (single statement per execute for asyncpg)
    op.execute("""
        UPDATE booking_proposals 
        SET state = 'expired', consumed_at = NOW() 
        WHERE challenge_id IS NULL AND state = 'pending';
    """)

    op.execute("""
        INSERT INTO consent_challenges (
            challenge_id, session_id, clinic_id, action_type, target_id, target_version,
            readback_details_hash, readback_completed_turn_id, status, created_at, expires_at, consumed_at
        )
        SELECT 
            bp.id, bp.session_id, bp.clinic_id, 'create_booking', bp.id::text, bp.expected_appointment_version,
            '0000000000000000000000000000000000000000000000000000000000000000', 0, 'consumed', bp.created_at, bp.expires_at, bp.consumed_at
        FROM booking_proposals bp
        WHERE bp.challenge_id IS NULL;
    """)

    op.execute("""
        UPDATE booking_proposals bp 
        SET challenge_id = bp.id,
            readback_details_hash = '0000000000000000000000000000000000000000000000000000000000000000'
        WHERE bp.challenge_id IS NULL;
    """)

    op.alter_column(
        "booking_proposals",
        "challenge_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "booking_proposals",
        "readback_details_hash",
        existing_type=sa.String(64),
        nullable=False,
    )

    op.create_unique_constraint(
        "uq_booking_proposals_challenge_id", "booking_proposals", ["challenge_id"]
    )
    op.create_foreign_key(
        "fk_booking_proposals_challenge_id",
        "booking_proposals",
        "consent_challenges",
        ["challenge_id"],
        ["challenge_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_booking_proposals_active_session",
        "booking_proposals",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending' AND proposal_type = 'new_booking'"),
    )
    op.create_index(
        "uq_reschedule_proposals_active_apt",
        "booking_proposals",
        ["appointment_id"],
        unique=True,
        postgresql_where=sa.text(
            "state = 'pending' AND proposal_type = 'reschedule' AND appointment_id IS NOT NULL"
        ),
    )

    # -------------------------------------------------------------------------
    # 7. Create cancellation_proposals table
    # -------------------------------------------------------------------------
    op.create_table(
        "cancellation_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "clinic_id",
            sa.Integer(),
            sa.ForeignKey("business_profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expected_appointment_version", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("created_turn_id", sa.Integer(), nullable=False),
        sa.Column("caller_name", sa.String(200), nullable=False),
        sa.Column("caller_phone", sa.String(30), nullable=False),
        sa.Column("reason_category", sa.String(50), nullable=False),
        sa.Column("state", sa.String(50), server_default="pending", nullable=False),
        sa.Column(
            "challenge_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consent_challenges.challenge_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("readback_details_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_cancellation_proposals_idempotency_key"),
        sa.UniqueConstraint("challenge_id", name="uq_cancellation_proposals_challenge_id"),
        sa.CheckConstraint(
            "state IN ('pending', 'confirmed', 'expired', 'replaced')",
            name="chk_cancellation_proposals_state",
        ),
        sa.CheckConstraint(
            "reason_category IN ('scheduling_conflict', 'patient_request', 'provider_unavailable', 'no_show', 'other')",
            name="chk_cancellation_proposals_reason_category",
        ),
        sa.CheckConstraint("created_turn_id > 0", name="chk_cancellation_proposal_turn"),
        sa.CheckConstraint("expires_at > created_at", name="chk_cancellation_proposal_expiry"),
    )
    op.create_index(
        "ix_cancellation_proposals_session",
        "cancellation_proposals",
        ["session_id"],
    )
    op.create_index(
        "ix_cancellation_proposals_expires",
        "cancellation_proposals",
        ["expires_at"],
    )
    op.create_index(
        "uq_cancellation_proposals_active_apt",
        "cancellation_proposals",
        ["appointment_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "uq_cancellation_proposals_confirmed_apt",
        "cancellation_proposals",
        ["appointment_id"],
        unique=True,
        postgresql_where=sa.text("state = 'confirmed'"),
    )

    # -------------------------------------------------------------------------
    # 8. Create rate-limiting and security attempt tables
    # -------------------------------------------------------------------------
    op.create_table(
        "client_verification_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("attempt_type", sa.String(20), server_default="standard", nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_type IN ('standard', 'step_up')", name="chk_client_attempt_type"
        ),
    )
    op.create_index(
        "ix_client_attempts_window",
        "client_verification_attempts",
        ["client_id", "attempt_type", "attempted_at"],
    )

    op.create_table(
        "appointment_verification_failures",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_apt_failures_window",
        "appointment_verification_failures",
        ["appointment_id", "attempted_at"],
    )
    op.create_index(
        "ix_apt_failures_client",
        "appointment_verification_failures",
        ["appointment_id", "client_id"],
    )

    op.create_table(
        "token_issuance_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("client_ip_hash", sa.String(16), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_token_attempts_window",
        "token_issuance_attempts",
        ["client_ip_hash", "attempted_at"],
    )

    # -------------------------------------------------------------------------
    # 9. Create partial unique indexes on consent_challenges
    # -------------------------------------------------------------------------
    op.drop_index("uq_consent_challenge_active_session", table_name="consent_challenges")
    op.create_index(
        "uq_consent_challenge_active_session",
        "consent_challenges",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending_readback', 'pending', 'authorized')"),
    )
    op.create_index(
        "uq_consent_challenge_authorized_turn",
        "consent_challenges",
        ["session_id", "consumed_turn_id"],
        unique=True,
        postgresql_where=sa.text(
            "consumed_turn_id IS NOT NULL AND status IN ('authorized', 'consumed')"
        ),
    )
    op.create_index(
        "uq_consent_challenge_consumed_item",
        "consent_challenges",
        ["session_id", "consumed_item_id"],
        unique=True,
        postgresql_where=sa.text(
            "consumed_item_id IS NOT NULL AND status IN ('authorized', 'consumed')"
        ),
    )


def downgrade() -> None:
    # 1. Drop partial unique indexes on consent_challenges
    op.drop_index("uq_consent_challenge_consumed_item", table_name="consent_challenges")
    op.drop_index("uq_consent_challenge_authorized_turn", table_name="consent_challenges")
    op.drop_index("uq_consent_challenge_active_session", table_name="consent_challenges")

    # 2. Restore Phase 4 active session unique index
    op.create_index(
        "uq_consent_challenge_active_session",
        "consent_challenges",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # 3. Drop rate limiting tables (DATA LOSS: All verification & token rate-limit history deleted)
    op.drop_table("token_issuance_attempts")
    op.drop_table("appointment_verification_failures")
    op.drop_table("client_verification_attempts")

    # 4. Drop cancellation_proposals table (DATA LOSS: All cancellation proposals deleted)
    op.drop_table("cancellation_proposals")

    # 5. Drop booking_proposals additions
    op.drop_index("uq_reschedule_proposals_active_apt", table_name="booking_proposals")
    op.drop_index("uq_booking_proposals_active_session", table_name="booking_proposals")
    op.drop_constraint("fk_booking_proposals_challenge_id", "booking_proposals", type_="foreignkey")
    op.drop_constraint("uq_booking_proposals_challenge_id", "booking_proposals", type_="unique")
    op.drop_column("booking_proposals", "readback_details_hash")
    op.drop_column("booking_proposals", "challenge_id")

    # Revert booking_proposals state constraints to Phase 4 baseline
    op.execute("UPDATE booking_proposals SET state = 'expired' WHERE state = 'replaced';")
    op.drop_constraint("chk_proposal_state_linkage", "booking_proposals", type_="check")
    op.drop_constraint("chk_proposal_state", "booking_proposals", type_="check")
    op.create_check_constraint(
        "chk_proposal_state",
        "booking_proposals",
        "state IN ('pending', 'confirmed', 'expired', 'rejected')",
    )
    op.create_check_constraint(
        "chk_proposal_state_linkage",
        "booking_proposals",
        "("
        "(state = 'pending' AND confirmed_appointment_id IS NULL AND consumed_at IS NULL) "
        "OR "
        "(state = 'confirmed' AND confirmed_appointment_id IS NOT NULL AND consumed_at IS NOT NULL) "
        "OR "
        "(state IN ('expired', 'rejected') AND confirmed_appointment_id IS NULL AND consumed_at IS NOT NULL)"
        ")",
    )

    # 6. Revert consent_challenges constraints & safe backfill for NOT NULL
    op.drop_constraint("chk_consent_challenge_action_type", "consent_challenges", type_="check")
    op.drop_constraint("chk_consent_challenge_status", "consent_challenges", type_="check")
    op.create_check_constraint(
        "chk_consent_challenge_status",
        "consent_challenges",
        "status IN ('pending', 'authorized', 'invalidated', 'consumed')",
    )

    # Irreversible State Transformation:
    # Map Phase 5 'pending_readback' to Phase 4 'invalidated' with dummy turn_id = 0 to satisfy NOT NULL
    op.execute("""
        UPDATE consent_challenges
        SET status = 'invalidated',
            readback_completed_turn_id = 0
        WHERE readback_completed_turn_id IS NULL OR status = 'pending_readback';
    """)
    op.alter_column(
        "consent_challenges",
        "readback_completed_turn_id",
        existing_type=sa.Integer(),
        nullable=False,
    )

    # 7. Drop Phase 5 columns from consent_challenges
    op.drop_column("consent_challenges", "consumed_item_id")
    op.drop_column("consent_challenges", "readback_completed_at")
    op.drop_column("consent_challenges", "invalidation_reason")
    op.drop_column("consent_challenges", "invalidated_at")
    op.drop_column("consent_challenges", "authorized_at")
    op.drop_column("consent_challenges", "authorization_signature")
    op.drop_column("consent_challenges", "authorization_nonce")

    # 8. Drop caller_verification_sessions additions
    op.drop_column("caller_verification_sessions", "handoff_category")
    op.drop_column("caller_verification_sessions", "handed_off_at")
    op.drop_column("caller_verification_sessions", "handed_off")
