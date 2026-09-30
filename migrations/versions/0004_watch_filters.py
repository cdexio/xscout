"""Per-tag content filters on watch items (owner request 2026-09-30).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("watch_items", sa.Column("filters", pg.JSONB(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("watch_items", "filters")
