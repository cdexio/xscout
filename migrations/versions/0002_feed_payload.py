"""Feed entries carry the normalized tweet (plan 5.3).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feed_entries", sa.Column("payload", pg.JSONB(), nullable=False, server_default="{}"))
    op.create_index("ix_feed_entries_watch_item_id", "feed_entries", ["watch_item_id"])


def downgrade() -> None:
    op.drop_index("ix_feed_entries_watch_item_id", table_name="feed_entries")
    op.drop_column("feed_entries", "payload")
