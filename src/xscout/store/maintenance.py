"""Key/value state, retention and sample access (plan 6.2-6.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xscout.config import RetentionTunables
from xscout.store.models import CacheEntry, FeedEntry, KvState, RequestLog, ResponseSample, Tweet


class KvRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def get(self, key: str) -> Any | None:
        async with self._sessions() as s:
            row = await s.get(KvState, key)
            return row.value if row else None

    async def set(self, key: str, value: Any) -> None:
        stmt = insert(KvState).values(key=key, value=value, updated_at=datetime.now(UTC))
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at}
        )
        async with self._sessions.begin() as s:
            await s.execute(stmt)


class RetentionRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def prune(self, cfg: RetentionTunables, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(UTC)
        plan = [
            ("tweets", delete(Tweet).where(Tweet.fetched_at < now - timedelta(days=cfg.tweets_days))),
            ("feed_entries", delete(FeedEntry).where(FeedEntry.created_at < now - timedelta(days=cfg.tweets_days))),
            (
                "response_samples",
                delete(ResponseSample).where(ResponseSample.created_at < now - timedelta(days=cfg.samples_days)),
            ),
            ("request_log", delete(RequestLog).where(RequestLog.at < now - timedelta(days=cfg.request_log_days))),
            ("cache_entries", delete(CacheEntry).where(CacheEntry.expires_at < now)),
        ]
        out: dict[str, int] = {}
        async with self._sessions.begin() as s:
            for name, stmt in plan:
                out[name] = (await s.execute(stmt)).rowcount or 0
        return out


class SampleReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(
                        ResponseSample.id,
                        ResponseSample.created_at,
                        ResponseSample.operation,
                        ResponseSample.bucket,
                        ResponseSample.status,
                        ResponseSample.reason,
                    )
                    .order_by(ResponseSample.id.desc())
                    .limit(limit)
                )
            ).all()
            return [dict(r._mapping) for r in rows]

    async def body(self, sample_id: int) -> str | None:
        async with self._sessions() as s:
            row = await s.get(ResponseSample, sample_id)
            return row.body if row else None
