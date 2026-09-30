"""ORM models for the tables in spec §9."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AccountStatus(enum.StrEnum):
    ACTIVE = "active"
    LOCKED = "locked"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    auth_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    ct0_enc: Mapped[str] = mapped_column(Text, nullable=False)
    proxy_enc: Mapped[str | None] = mapped_column(Text)
    impersonate: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AccountStatus.ACTIVE)
    status_reason: Mapped[str | None] = mapped_column(Text)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    allow_overflow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cooling_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RateLimitState(Base):
    """Quota per (account, operation, bucket); bucket = method x bearer, e.g. POST/main."""

    __tablename__ = "rate_limit_state"

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), primary_key=True)
    limit: Mapped[int | None] = mapped_column(Integer)
    remaining: Mapped[int | None] = mapped_column(Integer)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooling_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Operation(Base):
    """GraphQL operation registry with history (one row per seen query id)."""

    __tablename__ = "operations"
    __table_args__ = (UniqueConstraint("name", "query_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    query_id: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_switches: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    field_toggles: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    features: Mapped[dict | None] = mapped_column(JSONB)
    method: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # discovery | fallback | manual
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    bio: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followers: Mapped[int | None] = mapped_column(BigInteger)
    following: Mapped[int | None] = mapped_column(BigInteger)
    tweet_count: Mapped[int | None] = mapped_column(BigInteger)
    verified_type: Mapped[str | None] = mapped_column(String(32))
    is_blue_verified: Mapped[bool | None] = mapped_column(Boolean)
    protected: Mapped[bool | None] = mapped_column(Boolean)
    location: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = _now()


class Tweet(Base):
    __tablename__ = "tweets"
    __table_args__ = (Index("ix_tweets_author_created", "author_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    author_id: Mapped[int | None] = mapped_column(BigInteger)
    author_username: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lang: Mapped[str | None] = mapped_column(String(16))
    reply_count: Mapped[int | None] = mapped_column(BigInteger)
    retweet_count: Mapped[int | None] = mapped_column(BigInteger)
    quote_count: Mapped[int | None] = mapped_column(BigInteger)
    like_count: Mapped[int | None] = mapped_column(BigInteger)
    view_count: Mapped[int | None] = mapped_column(BigInteger)
    bookmark_count: Mapped[int | None] = mapped_column(BigInteger)
    is_reply: Mapped[bool | None] = mapped_column(Boolean)
    is_retweet: Mapped[bool | None] = mapped_column(Boolean)
    is_quote: Mapped[bool | None] = mapped_column(Boolean)
    quoted_id: Mapped[int | None] = mapped_column(BigInteger)
    retweeted_id: Mapped[int | None] = mapped_column(BigInteger)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger)
    hashtags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    cashtags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    mentions: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    urls: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    media: Mapped[list | None] = mapped_column(JSONB)
    url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = _now()


class WatchItem(Base):
    __tablename__ = "watch_items"
    __table_args__ = (UniqueConstraint("kind", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # user | query
    value: Mapped[str] = mapped_column(Text, nullable=False)
    interval_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    consumers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    filters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # tag -> WatchFilter
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_tweet_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FeedEntry(Base):
    __tablename__ = "feed_entries"
    __table_args__ = (
        UniqueConstraint("tweet_id", "watch_item_id"),
        Index("ix_feed_entries_tags", "tags", postgresql_using="gin"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tweet_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    watch_item_id: Mapped[int] = mapped_column(
        ForeignKey("watch_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _now()


class CacheEntry(Base):
    __tablename__ = "cache_entries"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ResponseSample(Base):
    __tablename__ = "response_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str | None] = mapped_column(String(16))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    status: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class KvState(Base):
    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RequestLog(Base):
    __tablename__ = "request_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    consumer: Mapped[str | None] = mapped_column(String(32))
    priority: Mapped[str | None] = mapped_column(String(4))
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str | None] = mapped_column(String(16))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    status: Mapped[int | None] = mapped_column(Integer)
    x_error_codes: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(32))
