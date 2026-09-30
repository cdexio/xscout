"""Watch items and feed entries (spec §8, plan 5.1, 5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xscout.scheduler.units import WatchView
from xscout.store.models import FeedEntry, WatchItem


def _view(row: WatchItem) -> WatchView:
    return WatchView(
        id=row.id,
        kind=row.kind,
        value=row.value,
        interval_sec=row.interval_sec,
        tags=list(row.tags or []),
        enabled=row.enabled,
        next_run_at=row.next_run_at,
        last_seen_tweet_id=row.last_seen_tweet_id,
        created_at=row.created_at,
        filters=dict(row.filters or {}),
    )


def item_json(row: WatchItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "value": row.value,
        "interval_sec": row.interval_sec,
        "tags": list(row.tags or []),
        "consumers": list(row.consumers or []),
        "filters": dict(row.filters or {}),
        "enabled": row.enabled,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "last_seen_tweet_id": str(row.last_seen_tweet_id) if row.last_seen_tweet_id else None,
        "last_error": row.last_error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@dataclass(frozen=True)
class RunUpdate:
    item_id: int
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_seen_tweet_id: int | None = None
    error: str | None = None


class WatchRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def upsert(
        self,
        kind: str,
        value: str,
        interval_sec: int,
        tags: list[str],
        consumer: str,
        filters: dict[str, dict] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create, or merge into the existing item for (kind, value): shortest interval, union of tags/consumers.

        `filters` (tag -> filter) replaces the filter of those tags only; other tags keep theirs.
        """
        async with self._sessions.begin() as s:
            row = await s.scalar(select(WatchItem).where(WatchItem.kind == kind, WatchItem.value == value))
            created = row is None
            if row is None:
                row = WatchItem(
                    kind=kind,
                    value=value,
                    interval_sec=interval_sec,
                    tags=sorted(set(tags)),
                    consumers=[consumer],
                    filters=dict(filters or {}),
                    enabled=True,
                )
                s.add(row)
            else:
                row.interval_sec = min(row.interval_sec, interval_sec)
                row.tags = sorted(set(row.tags or []) | set(tags))
                row.consumers = sorted(set(row.consumers or []) | {consumer})
                if filters:
                    row.filters = {**(row.filters or {}), **filters}
                row.enabled = True
            await s.flush()
            await s.refresh(row)
            return item_json(row), created

    async def list(self, consumer: str | None = None, tag: str | None = None) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            stmt = select(WatchItem).order_by(WatchItem.id)
            if consumer:
                stmt = stmt.where(WatchItem.consumers.contains([consumer]))
            if tag:
                stmt = stmt.where(WatchItem.tags.contains([tag]))
            return [item_json(r) for r in (await s.scalars(stmt)).all()]

    async def get(self, item_id: int) -> dict[str, Any] | None:
        async with self._sessions() as s:
            row = await s.get(WatchItem, item_id)
            return item_json(row) if row else None

    async def patch(
        self,
        item_id: int,
        interval_sec: int | None,
        tags: list[str] | None,
        enabled: bool | None,
        filters: dict[str, dict | None] | None = None,
    ) -> dict[str, Any] | None:
        """`filters`: tag -> filter to set, or tag -> None to clear that tag's filter."""
        async with self._sessions.begin() as s:
            row = await s.get(WatchItem, item_id)
            if row is None:
                return None
            if interval_sec is not None:
                row.interval_sec = interval_sec
            if tags is not None:
                row.tags = sorted(set(tags))
            if enabled is not None:
                row.enabled = enabled
            if filters is not None:
                merged = dict(row.filters or {})
                for tag, spec in filters.items():
                    if spec is None:
                        merged.pop(tag, None)
                    else:
                        merged[tag] = spec
                row.filters = merged
            await s.flush()
            await s.refresh(row)
            return item_json(row)

    async def detach(self, item_id: int, consumer: str) -> str | None:
        """Remove the caller's interest; delete the item when no consumer is left. Returns the action taken."""
        async with self._sessions.begin() as s:
            row = await s.get(WatchItem, item_id)
            if row is None:
                return None
            left = sorted(set(row.consumers or []) - {consumer})
            if left:
                row.consumers = left
                return "detached"
            await s.delete(row)
            return "deleted"

    async def enabled_views(self) -> list[WatchView]:
        async with self._sessions() as s:
            rows = (await s.scalars(select(WatchItem).where(WatchItem.enabled))).all()
            return [_view(r) for r in rows]

    async def all_views(self) -> list[WatchView]:
        async with self._sessions() as s:
            return [_view(r) for r in (await s.scalars(select(WatchItem))).all()]

    async def apply_runs(self, updates: list[RunUpdate]) -> None:
        if not updates:
            return
        async with self._sessions.begin() as s:
            for u in updates:
                row = await s.get(WatchItem, u.item_id)
                if row is None:
                    continue
                row.next_run_at = u.next_run_at
                if u.last_run_at is not None:
                    row.last_run_at = u.last_run_at
                if u.last_seen_tweet_id is not None:
                    row.last_seen_tweet_id = max(row.last_seen_tweet_id or 0, u.last_seen_tweet_id)
                row.last_error = u.error


@dataclass(frozen=True)
class NewEntry:
    tweet_id: int
    watch_item_id: int
    tags: list[str]
    payload: dict[str, Any]


class FeedRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def append(self, entries: list[NewEntry]) -> int:
        if not entries:
            return 0
        rows = [
            {"tweet_id": e.tweet_id, "watch_item_id": e.watch_item_id, "tags": e.tags, "payload": e.payload}
            for e in entries
        ]
        stmt = (
            insert(FeedEntry)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["tweet_id", "watch_item_id"])
            .returning(FeedEntry.seq)
        )
        async with self._sessions.begin() as s:
            return len((await s.execute(stmt)).all())

    async def since(self, seq: int, tags: list[str] | None, limit: int) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            stmt = select(FeedEntry).where(FeedEntry.seq > seq).order_by(FeedEntry.seq).limit(limit)
            if tags:
                stmt = stmt.where(FeedEntry.tags.overlap(tags))
            rows = (await s.scalars(stmt)).all()
            return [
                {
                    "seq": r.seq,
                    "watch_item_id": r.watch_item_id,
                    "tags": list(r.tags or []),
                    "created_at": r.created_at.isoformat(),
                    "tweet": r.payload,
                }
                for r in rows
            ]

    async def last_seq(self) -> int:
        async with self._sessions() as s:
            return int(await s.scalar(select(func.coalesce(func.max(FeedEntry.seq), 0))))

    async def prune_before(self, before: datetime) -> int:
        async with self._sessions.begin() as s:
            res = await s.execute(delete(FeedEntry).where(FeedEntry.created_at < before))
            return res.rowcount or 0
