"""initial_baseline

Revision ID: 12ddd6604a2a
Revises: 
Create Date: 2026-11-07 18:44:43.263674

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '12ddd6604a2a'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
