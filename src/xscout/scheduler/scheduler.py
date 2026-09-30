"""Watchlist scheduler: one poll serves every bot; results go to the feed (spec §8, plan 5.2-5.3).

Watermarks are tweet ids (snowflakes, time-ordered). After a scan, every item of the unit has "seen
everything up to the newest id fetched", so quiet accounts do not force deep paging on the next poll.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from xscout.config import Tunables
from xscout.gateway.gateway import BadRequest, Gateway, Unavailable
from xscout.pool.budget import Priority
from xscout.scheduler.filters import passing_tags
from xscout.scheduler.units import Unit, WatchView, build_units
from xscout.store.watch import FeedRepository, NewEntry, RunUpdate, WatchRepository
from xscout.xweb import ops
from xscout.xweb.models import Tweet, TweetPage

log = logging.getLogger("xscout.scheduler")

CONSUMER = "scheduler"
Archive = Callable[[list[Tweet], list], Awaitable[Any]]


@dataclass
class PollResult:
    unit: str
    pages: int
    fetched: int
    new_entries: int
    error: str | None = None


class FeedSignal:
    """Wakes long-polling feed readers when the scheduler appends entries."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self.version = 0

    async def bump(self) -> None:
        async with self._cond:
            self.version += 1
            self._cond.notify_all()

    async def wait(self, since_version: int, within_sec: float) -> bool:
        async with self._cond:
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: self.version != since_version), within_sec)
                return True
            except TimeoutError:
                return False


class WatchScheduler:
    def __init__(
        self,
        gateway: Gateway,
        watches: WatchRepository,
        feed: FeedRepository,
        archive: Archive | None,
        tunables: Tunables,
        signal: FeedSignal | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.gateway = gateway
        self.watches = watches
        self.feed = feed
        self.archive = archive
        self.t = tunables
        self.signal = signal or FeedSignal()
        self.clock = clock
        self.units: list[Unit] = []
        self._loaded_at = 0.0
        self._dirty = True
        self._running: set[str] = set()
        self._sem = asyncio.Semaphore(tunables.watch.max_concurrent_polls)
        self._tasks: set[asyncio.Task] = set()
        self.stats = {"polls": 0, "entries": 0, "errors": 0}

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def mark_dirty(self) -> None:
        self._dirty = True

    async def reload(self) -> None:
        items = await self.watches.enabled_views()
        self.units = build_units(items, self.t.watch.max_query_chars)
        self._loaded_at = self.clock()
        self._dirty = False

    async def tick(self) -> list[asyncio.Task]:
        if self._dirty or self.clock() - self._loaded_at >= self.t.watch.reload_sec:
            await self.reload()
        now = self._now()
        due = [u for u in self.units if u.key not in self._running and u.due(now)]
        due.sort(key=lambda u: u.overdue_since(now), reverse=True)
        started = []
        for unit in due:
            self._running.add(unit.key)
            task = asyncio.get_running_loop().create_task(self._guarded(unit))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started.append(task)
        return started

    async def _guarded(self, unit: Unit) -> PollResult:
        try:
            async with self._sem:
                return await self.poll(unit)
        finally:
            self._running.discard(unit.key)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.warning("scheduler tick failed", extra={"fields": {"error": str(e)}})
            await asyncio.sleep(self.t.watch.tick_sec)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # MARK: polling

    async def poll(self, unit: Unit) -> PollResult:
        self.stats["polls"] += 1
        started = self._now()
        try:
            tweets, includes, pages = await self._scan(unit)
        except Unavailable as e:
            retry_in = min(unit.interval_sec, max(5, e.retry_after_sec))
            return await self._failed(unit, f"unavailable: {e.reason}", retry_in)
        except BadRequest as e:
            if unit.kind == "user":
                return await self._fallback_user_tweets(unit, str(e))
            return await self._failed(unit, f"bad query: {e}", unit.interval_sec)
        entries = self._select(unit, tweets)
        written = await self._emit(tweets, includes, entries)
        newest = max((t.id for t in tweets), default=None)
        next_run = started + timedelta(seconds=unit.interval_sec)
        await self.watches.apply_runs([RunUpdate(i.id, next_run, started, newest, None) for i in unit.items])
        for i in unit.items:  # keep the in-memory view current until the next reload
            i.next_run_at = next_run
            if newest is not None:
                i.last_seen_tweet_id = max(i.last_seen_tweet_id or 0, newest)
        return PollResult(unit.key, pages, len(tweets), written)

    async def _scan(self, unit: Unit) -> tuple[list[Tweet], list[Tweet], int]:
        watermarks = [i.last_seen_tweet_id for i in unit.items]
        floor = min(watermarks) if all(w is not None for w in watermarks) else None
        tweets: dict[int, Tweet] = {}
        includes: dict[int, Tweet] = {}
        cursor = None
        pages = 0
        while pages < self.t.watch.max_pages_per_poll:
            result = await self.gateway.run(
                lambda cur: ops.search(unit.query, "latest", cursor=cur),
                priority=Priority.P1,
                consumer=CONSUMER,
                cursor=cursor,
                max_age_sec=0,
            )
            pages += 1
            page = result.pages[0] if result.pages else None
            if not isinstance(page, TweetPage) or not page.items:
                break
            for t in page.items:
                tweets.setdefault(t.id, t)
            for t in page.includes:
                includes.setdefault(t.id, t)
            oldest = min(t.id for t in page.items)
            if floor is None or oldest <= floor or not page.cursor_bottom:
                break
            cursor = page.cursor_bottom
        else:
            log.info("poll hit the page cap; older tweets may be skipped", extra={"fields": {"unit": unit.key}})
        return sorted(tweets.values(), key=lambda t: t.id), list(includes.values()), pages

    def _select(self, unit: Unit, tweets: list[Tweet]) -> list[tuple[Tweet, WatchView, list[str]]]:
        """New tweets per item, with the item's tags whose filter they pass (items without tags: no filter)."""
        by_user = {i.value: i for i in unit.items} if unit.kind == "user" else {}
        out: list[tuple[Tweet, WatchView, list[str]]] = []
        for t in tweets:
            if unit.kind == "user":
                name = (t.author.username or "").lower() if t.author else ""
                item = by_user.get(name)
                if item is None:
                    continue
            else:
                item = unit.items[0]
            if item.last_seen_tweet_id is not None:
                if t.id <= item.last_seen_tweet_id:
                    continue
            else:  # first run: only recent tweets, not the account's whole history
                since = (item.created_at or self._now()) - timedelta(seconds=item.interval_sec)
                if t.created_at is None or t.created_at < since:
                    continue
            tags = passing_tags(t, item.tags, item.filters)
            if item.tags and not tags:
                self.stats["filtered"] = self.stats.get("filtered", 0) + 1
                continue
            out.append((t, item, tags))
        return out

    async def _emit(
        self, tweets: list[Tweet], includes: list[Tweet], entries: list[tuple[Tweet, WatchView, list[str]]]
    ) -> int:
        if self.archive is not None and (tweets or includes):
            try:
                await self.archive(tweets + includes, [])
            except Exception as e:
                log.warning("archive failed", extra={"fields": {"error": str(e)}})
        written = await self.feed.append(
            [NewEntry(t.id, item.id, tags, t.model_dump(mode="json")) for t, item, tags in entries]
        )
        if written:
            self.stats["entries"] += written
            await self.signal.bump()
        return written

    async def _failed(self, unit: Unit, error: str, retry_in: float) -> PollResult:
        self.stats["errors"] += 1
        next_run = self._now() + timedelta(seconds=retry_in)
        await self.watches.apply_runs([RunUpdate(i.id, next_run, None, None, error[:500]) for i in unit.items])
        for i in unit.items:
            i.next_run_at = next_run
        log.info("poll failed", extra={"fields": {"unit": unit.key, "error": error, "retry_in": retry_in}})
        return PollResult(unit.key, 0, 0, 0, error)

    async def _fallback_user_tweets(self, unit: Unit, reason: str) -> PollResult:
        """Batch query rejected: poll each user through UserTweets instead."""
        fields = {"unit": unit.key, "reason": reason}
        log.warning("batch query rejected; falling back to UserTweets", extra={"fields": fields})
        total_new = fetched = 0
        started = self._now()
        for item in unit.items:
            single = Unit(f"{unit.key}:{item.value}", "user", "", item.interval_sec, [item])
            try:
                profile = await self.gateway.run(
                    lambda _c, v=item.value: ops.user_by_screen_name(v),
                    priority=Priority.P1,
                    consumer=CONSUMER,
                    max_age_sec=3600,
                )
                user = profile.pages[0] if profile.pages else None
                if user is None:
                    await self._failed(single, "user not found", item.interval_sec)
                    continue
                result = await self.gateway.run(
                    lambda cur, uid=user.id: ops.user_tweets(uid, cursor=cur),
                    priority=Priority.P1,
                    consumer=CONSUMER,
                    max_age_sec=0,
                )
            except (Unavailable, BadRequest) as e:
                await self._failed(single, f"fallback: {e}", item.interval_sec)
                continue
            page = result.pages[0] if result.pages else None
            tweets = sorted(page.items, key=lambda t: t.id) if isinstance(page, TweetPage) else []
            fetched += len(tweets)
            total_new += await self._emit(tweets, page.includes if page else [], self._select(single, tweets))
            newest = max((t.id for t in tweets), default=None)
            next_run = started + timedelta(seconds=item.interval_sec)
            note = f"batch rejected: {reason}"[:500]
            await self.watches.apply_runs([RunUpdate(item.id, next_run, started, newest, note)])
            item.next_run_at = next_run
        return PollResult(unit.key, len(unit.items), fetched, total_new)
