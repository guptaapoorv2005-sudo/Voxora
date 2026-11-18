"""Preserve the earlier revision ID without adding demo tables on new installs.

If an earlier version already created demo_sessions, this revision does not
delete it: migrations must not silently drop data from an existing database.
"""

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
