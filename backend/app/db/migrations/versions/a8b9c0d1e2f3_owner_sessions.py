"""Add durable owner refresh sessions.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
"""

import sqlalchemy as sa
from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_profile",
        sa.Column("receptionist_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("CREATE UNIQUE INDEX uq_single_owner ON owner_users ((true))")
    op.create_table(
        "owner_login_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "owner_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("owner_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("refresh_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_owner_sessions_owner_id", "owner_sessions", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_owner_sessions_owner_id", table_name="owner_sessions")
    op.drop_table("owner_sessions")
    op.drop_table("owner_login_attempts")
    op.execute("DROP INDEX uq_single_owner")
    op.drop_column("business_profile", "receptionist_enabled")
