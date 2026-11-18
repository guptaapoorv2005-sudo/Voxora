"""simplify_consent_and_locking

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Drop consumed_consent_nonces table and its index
    op.drop_index("ix_consumed_nonces_session", table_name="consumed_consent_nonces")
    op.drop_table("consumed_consent_nonces")

    # 2. Drop obsolete authorization columns from consent_challenges
    op.drop_column("consent_challenges", "authorization_nonce")
    op.drop_column("consent_challenges", "authorization_signature")
    op.drop_column("consent_challenges", "authorized_at")

    # 3. Update chk_consent_challenge_status constraint (remove 'authorized' state)
    op.drop_constraint("chk_consent_challenge_status", "consent_challenges", type_="check")
    op.create_check_constraint(
        "chk_consent_challenge_status",
        "consent_challenges",
        "status IN ('pending_readback', 'pending', 'invalidated', 'consumed')",
    )

    # 4. Update partial unique indexes on consent_challenges
    op.drop_index("uq_consent_challenge_active_session", table_name="consent_challenges")
    op.create_index(
        "uq_consent_challenge_active_session",
        "consent_challenges",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending_readback', 'pending')"),
    )

    op.drop_index("uq_consent_challenge_authorized_turn", table_name="consent_challenges")
    op.create_index(
        "uq_consent_challenge_authorized_turn",
        "consent_challenges",
        ["session_id", "consumed_turn_id"],
        unique=True,
        postgresql_where=sa.text("consumed_turn_id IS NOT NULL AND status = 'consumed'"),
    )

    op.drop_index("uq_consent_challenge_consumed_item", table_name="consent_challenges")
    op.create_index(
        "uq_consent_challenge_consumed_item",
        "consent_challenges",
        ["session_id", "consumed_item_id"],
        unique=True,
        postgresql_where=sa.text("consumed_item_id IS NOT NULL AND status = 'consumed'"),
    )


def downgrade() -> None:
    # 1. Re-add obsolete columns
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

    # 2. Restore check constraint
    op.drop_constraint("chk_consent_challenge_status", "consent_challenges", type_="check")
    op.create_check_constraint(
        "chk_consent_challenge_status",
        "consent_challenges",
        "status IN ('pending_readback', 'pending', 'authorized', 'invalidated', 'consumed')",
    )

    # 3. Restore indexes
    op.drop_index("uq_consent_challenge_consumed_item", table_name="consent_challenges")
    op.create_index(
        "uq_consent_challenge_consumed_item",
        "consent_challenges",
        ["session_id", "consumed_item_id"],
        unique=True,
        postgresql_where=sa.text(
            "consumed_item_id IS NOT NULL AND status IN ('authorized', 'consumed')"
        ),
    )

    op.drop_index("uq_consent_challenge_authorized_turn", table_name="consent_challenges")
    op.create_index(
        "uq_consent_challenge_authorized_turn",
        "consent_challenges",
        ["session_id", "consumed_turn_id"],
        unique=True,
        postgresql_where=sa.text(
            "consumed_turn_id IS NOT NULL AND status IN ('authorized', 'consumed')"
        ),
    )

    op.drop_index("uq_consent_challenge_active_session", table_name="consent_challenges")
    op.create_index(
        "uq_consent_challenge_active_session",
        "consent_challenges",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending_readback', 'pending', 'authorized')"),
    )

    # 4. Recreate consumed_consent_nonces table
    op.create_table(
        "consumed_consent_nonces",
        sa.Column("nonce", sa.String(128), primary_key=True),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_consumed_nonces_session",
        "consumed_consent_nonces",
        ["session_id"],
    )
