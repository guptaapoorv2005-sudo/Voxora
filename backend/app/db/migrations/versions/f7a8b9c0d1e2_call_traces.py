"""add call traces

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
"""
import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("calls", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("room", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(32), nullable=False))
    op.create_table("call_trace_events", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String(64), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.UniqueConstraint("call_id", "sequence", name="uq_call_trace_sequence"))
    op.create_index("ix_call_trace_events_call_id", "call_trace_events", ["call_id"])


def downgrade() -> None:
    op.drop_index("ix_call_trace_events_call_id", table_name="call_trace_events")
    op.drop_table("call_trace_events")
    op.drop_table("calls")
