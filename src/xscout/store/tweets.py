"""Upsert of fetched tweets and users (plan 4.2); the archive the bots and later backtests can use."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xscout.store.models import Tweet as TweetRow
from xscout.store.models import User as UserRow
from xscout.xweb.models import Tweet, User

USER_FIELDS = (
    "username",
    "display_name",
    "bio",
    "created_at",
    "followers",
    "following",
    "tweet_count",
    "verified_type",
    "is_blue_verified",
    "protected",
    "location",
    "avatar_url",
)


# Engagement counts change over time; identity fields do not.
MUTABLE_TWEET_FIELDS = (
    "text",
    "reply_count",
    "retweet_count",
    "quote_count",
    "like_count",
    "view_count",
    "bookmark_count",
    "author_username",
    "fetched_at",
)


def _user_row(u: User, now: datetime) -> dict:
    return {"id": u.id, **{f: getattr(u, f) for f in USER_FIELDS}, "fetched_at": now}


def _tweet_row(t: Tweet, now: datetime) -> dict:
    return {
        "id": t.id,
        "author_id": t.author.id if t.author else None,
        "author_username": t.author.username if t.author else None,
        "text": t.text,
        "created_at": t.created_at,
        "lang": t.lang,
        "reply_count": t.reply_count,
        "retweet_count": t.retweet_count,
        "quote_count": t.quote_count,
        "like_count": t.like_count,
        "view_count": t.view_count,
        "bookmark_count": t.bookmark_count,
        "is_reply": t.is_reply,
        "is_retweet": t.is_retweet,
        "is_quote": t.is_quote,
        "quoted_id": t.quoted_id,
        "retweeted_id": t.retweeted_id,
        "conversation_id": t.conversation_id,
        "hashtags": t.hashtags,
        "cashtags": t.cashtags,
        "mentions": t.mentions,
        "urls": t.urls,
        "media": [m.model_dump() for m in t.media],
        "url": t.url,
        "fetched_at": now,
    }


class TweetRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def upsert(self, tweets: list[Tweet], users: list[User] | None = None) -> tuple[int, int]:
        now = datetime.now(UTC)
        user_rows = {u.id: _user_row(u, now) for u in (users or []) if u.username}
        for t in tweets:
            if t.author is not None and t.author.username and t.author.id not in user_rows:
                user_rows[t.author.id] = _user_row(t.author, now)
        tweet_rows = {t.id: _tweet_row(t, now) for t in tweets}
        async with self._sessions.begin() as s:
            if user_rows:
                stmt = insert(UserRow).values(list(user_rows.values()))
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"], set_={f: stmt.excluded[f] for f in (*USER_FIELDS, "fetched_at")}
                )
                await s.execute(stmt)
            if tweet_rows:
                stmt = insert(TweetRow).values(list(tweet_rows.values()))
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"], set_={f: stmt.excluded[f] for f in MUTABLE_TWEET_FIELDS}
                )
                await s.execute(stmt)
        return len(tweet_rows), len(user_rows)
