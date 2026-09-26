"""Watchlist and feed use cases (spec §8, plan 5.1, 5.3)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from xscout.config import Tunables
from xscout.scheduler.scheduler import FeedSignal
from xscout.scheduler.units import WatchValueError, WatchView, build_units, normalize, projected_requests
from xscout.store.watch import FeedRepository, WatchRepository

TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_:.-]{0,31}$")
MAX_WAIT_SEC = 30


class OverCapacity(Exception):
    def __init__(self, needed: float, available: float):
        super().__init__(f"watchlist would need {needed:.0f} requests per 15 min; P1 capacity is {available:.0f}")
        self.needed = needed
        self.available = available


class WatchService:
    def __init__(
        self,
        watches: WatchRepository,
        feed: FeedRepository,
        signal: FeedSignal,
        tunables: Tunables,
        window_capacity: Callable[[], int],
        on_change: Callable[[], None] = lambda: None,
    ):
        self.watches = watches
        self.feed = feed
        self.signal = signal
        self.t = tunables
        self.window_capacity = window_capacity
        self.on_change = on_change

    # MARK: validation

    def _interval(self, value: int) -> int:
        lo, hi = self.t.watch.min_interval_sec, self.t.watch.max_interval_sec
        if not lo <= value <= hi:
            raise WatchValueError(f"interval_sec must be between {lo} and {hi}")
        return value

    @staticmethod
    def _tags(tags: list[str]) -> list[str]:
        out = []
        for tag in tags:
            t = tag.strip().lower()
            if not TAG_RE.fullmatch(t):
                raise WatchValueError(f"invalid tag {tag!r} (lowercase letters, digits, _ : . -; max 32)")
            out.append(t)
        return sorted(set(out))

    async def capacity_report(self, views: list[WatchView] | None = None) -> dict[str, float]:
        views = views if views is not None else await self.watches.enabled_views()
        needed = projected_requests(build_units(views, self.t.watch.max_query_chars))
        available = self.window_capacity() * self.t.budget.p1_share
        return {"needed_per_15m": round(needed, 1), "p1_capacity_per_15m": round(available, 1)}

    async def _check(self, views: list[WatchView]) -> None:
        report = await self.capacity_report(views)
        if report["needed_per_15m"] > report["p1_capacity_per_15m"]:
            raise OverCapacity(report["needed_per_15m"], report["p1_capacity_per_15m"])

    # MARK: use cases

    async def create(
        self, kind: str, value: str, interval_sec: int, tags: list[str], consumer: str
    ) -> tuple[dict, bool]:
        norm = normalize(kind, value, self.t.watch.max_query_chars)
        interval = self._interval(interval_sec)
        clean_tags = self._tags(tags)
        views = await self.watches.enabled_views()
        existing = next((v for v in views if v.kind == kind and v.value == norm), None)
        if existing is not None:
            existing.interval_sec = min(existing.interval_sec, interval)
        else:
            views.append(WatchView(id=-1, kind=kind, value=norm, interval_sec=interval, tags=clean_tags))
        await self._check(views)
        item, created = await self.watches.upsert(kind, norm, interval, clean_tags, consumer)
        self.on_change()
        return item, created

    async def patch(
        self, item_id: int, interval_sec: int | None, tags: list[str] | None, enabled: bool | None
    ) -> dict | None:
        interval = self._interval(interval_sec) if interval_sec is not None else None
        clean_tags = self._tags(tags) if tags is not None else None
        if interval is not None or enabled:
            views = await self.watches.all_views()
            for v in views:
                if v.id == item_id:
                    v.interval_sec = interval or v.interval_sec
                    v.enabled = True if enabled else v.enabled
            await self._check([v for v in views if v.enabled])
        item = await self.watches.patch(item_id, interval, clean_tags, enabled)
        self.on_change()
        return item

    async def list(self, consumer: str | None, tag: str | None) -> list[dict]:
        return await self.watches.list(consumer, tag)

    async def get(self, item_id: int) -> dict | None:
        return await self.watches.get(item_id)

    async def delete(self, item_id: int, consumer: str) -> str | None:
        action = await self.watches.detach(item_id, consumer)
        self.on_change()
        return action

    async def read_feed(self, since: int, tags: list[str] | None, limit: int, wait_sec: float) -> dict[str, Any]:
        wait = max(0.0, min(float(wait_sec), MAX_WAIT_SEC))
        version = self.signal.version
        rows = await self.feed.since(since, tags, limit)
        if not rows and wait > 0 and await self.signal.wait(version, wait):
            rows = await self.feed.since(since, tags, limit)
        return {"data": rows, "next_since": rows[-1]["seq"] if rows else since}
