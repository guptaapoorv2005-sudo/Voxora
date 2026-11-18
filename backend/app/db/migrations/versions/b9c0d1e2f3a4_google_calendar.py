"""Store Google Calendar connection and synchronization state."""

import sqlalchemy as sa
from alembic import op

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_calendar_connections",
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("event_prefix", sa.String(32), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
    )
    op.create_table(
        "google_calendar_events",
        sa.Column("appointment_id", sa.Integer(), sa.ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("google_calendar_connections.owner_id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("synced_version", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("google_calendar_events")
    op.drop_table("google_calendar_connections")
