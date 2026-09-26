"""Initial schema (spec §9).

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False, unique=True),
        sa.Column("auth_token_enc", sa.Text(), nullable=False),
        sa.Column("ct0_enc", sa.Text(), nullable=False),
        sa.Column("proxy_enc", sa.Text()),
        sa.Column("impersonate", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("status_reason", sa.Text()),
        sa.Column("status_changed_at", TS),
        sa.Column("allow_overflow", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cooling_until", TS),
        sa.Column("last_used_at", TS),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_error_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )

    op.create_table(
        "rate_limit_state",
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("operation", sa.String(64), primary_key=True),
        sa.Column("bucket", sa.String(16), primary_key=True),
        sa.Column("limit", sa.Integer()),
        sa.Column("remaining", sa.Integer()),
        sa.Column("reset_at", TS),
        sa.Column("cooling_until", TS),
        sa.Column("last_used_at", TS),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )

    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("query_id", sa.String(64), nullable=False),
        sa.Column("feature_switches", pg.ARRAY(sa.Text())),
        sa.Column("field_toggles", pg.ARRAY(sa.Text())),
        sa.Column("features", pg.JSONB()),
        sa.Column("method", sa.String(8)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", TS, nullable=False, server_default=NOW),
        sa.Column("last_seen_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("name", "query_id"),
    )
    op.create_index("ix_operations_name", "operations", ["name"])

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("bio", sa.Text()),
        sa.Column("created_at", TS),
        sa.Column("followers", sa.BigInteger()),
        sa.Column("following", sa.BigInteger()),
        sa.Column("tweet_count", sa.BigInteger()),
        sa.Column("verified_type", sa.String(32)),
        sa.Column("is_blue_verified", sa.Boolean()),
        sa.Column("protected", sa.Boolean()),
        sa.Column("location", sa.Text()),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("fetched_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_users_username", "users", ["username"])

    op.create_table(
        "tweets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("author_id", sa.BigInteger()),
        sa.Column("author_username", sa.String(32)),
        sa.Column("text", sa.Text()),
        sa.Column("created_at", TS),
        sa.Column("lang", sa.String(16)),
        sa.Column("reply_count", sa.BigInteger()),
        sa.Column("retweet_count", sa.BigInteger()),
        sa.Column("quote_count", sa.BigInteger()),
        sa.Column("like_count", sa.BigInteger()),
        sa.Column("view_count", sa.BigInteger()),
        sa.Column("bookmark_count", sa.BigInteger()),
        sa.Column("is_reply", sa.Boolean()),
        sa.Column("is_retweet", sa.Boolean()),
        sa.Column("is_quote", sa.Boolean()),
        sa.Column("quoted_id", sa.BigInteger()),
        sa.Column("retweeted_id", sa.BigInteger()),
        sa.Column("conversation_id", sa.BigInteger()),
        sa.Column("hashtags", pg.ARRAY(sa.Text())),
        sa.Column("cashtags", pg.ARRAY(sa.Text())),
        sa.Column("mentions", pg.ARRAY(sa.Text())),
        sa.Column("urls", pg.ARRAY(sa.Text())),
        sa.Column("media", pg.JSONB()),
        sa.Column("url", sa.Text()),
        sa.Column("fetched_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_tweets_created_at", "tweets", ["created_at"])
    op.create_index("ix_tweets_author_created", "tweets", ["author_id", "created_at"])

    op.create_table(
        "watch_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("interval_sec", sa.Integer(), nullable=False),
        sa.Column("tags", pg.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("consumers", pg.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", TS),
        sa.Column("last_run_at", TS),
        sa.Column("last_seen_tweet_id", sa.BigInteger()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("kind", "value"),
    )
    op.create_index("ix_watch_items_next_run_at", "watch_items", ["next_run_at"])

    op.create_table(
        "feed_entries",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tweet_id", sa.BigInteger(), nullable=False),
        sa.Column("watch_item_id", sa.Integer(), sa.ForeignKey("watch_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tags", pg.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("tweet_id", "watch_item_id"),
    )
    op.create_index("ix_feed_entries_tags", "feed_entries", ["tags"], postgresql_using="gin")

    op.create_table(
        "cache_entries",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
    )
    op.create_index("ix_cache_entries_expires_at", "cache_entries", ["expires_at"])

    op.create_table(
        "response_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(16)),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("status", sa.Integer()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("body", sa.Text()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_response_samples_created_at", "response_samples", ["created_at"])

    op.create_table(
        "request_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", TS, nullable=False, server_default=NOW),
        sa.Column("consumer", sa.String(32)),
        sa.Column("priority", sa.String(4)),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(16)),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("status", sa.Integer()),
        sa.Column("x_error_codes", pg.ARRAY(sa.Integer())),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("outcome", sa.String(32)),
    )
    op.create_index("ix_request_log_at", "request_log", ["at"])


def downgrade() -> None:
    for table in (
        "request_log",
        "response_samples",
        "cache_entries",
        "feed_entries",
        "watch_items",
        "tweets",
        "users",
        "operations",
        "rate_limit_state",
        "accounts",
    ):
        op.drop_table(table)
