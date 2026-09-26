"""Use cases behind the HTTP API (plan 4.2): limit -> pages, username -> id, envelopes, archiving."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from xscout.config import Tunables
from xscout.gateway.gateway import Gateway, Meta, Result
from xscout.pool.budget import Priority
from xscout.xweb import ops
from xscout.xweb.models import Tweet, TweetPage, User, UserPage

log = logging.getLogger("xscout.service")

PROFILE_MAX_AGE_SEC = 3600  # username -> id lookups reuse profiles up to an hour old


class NotFound(Exception):
    pass


@dataclass
class Envelope:
    data: Any
    meta: Meta
    next_cursor: str | None = None
    includes: list[Tweet] | None = None

    def to_json(self) -> dict:
        m = self.meta
        out: dict[str, Any] = {
            "data": _dump(self.data),
            "next_cursor": self.next_cursor,
            "meta": {
                "cached": m.cached,
                "stale": m.stale,
                "shared": m.shared,
                "age_sec": m.age_sec,
                "fetched_at": m.fetched_at.isoformat() if m.fetched_at else None,
                "pages_fetched": m.pages_fetched,
            },
        }
        if self.includes is not None:
            out["includes"] = _dump(self.includes)
        return out


def _dump(v: Any) -> Any:
    if isinstance(v, list):
        return [_dump(x) for x in v]
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    return v


Archive = Callable[[list[Tweet], list[User]], Awaitable[Any]]


class XService:
    def __init__(self, gateway: Gateway, tunables: Tunables, archive: Archive | None = None):
        self.gateway = gateway
        self.t = tunables
        self._archive = archive
        self._tasks: set[asyncio.Task] = set()

    def pages_for(self, limit: int) -> int:
        return max(1, min(self.t.api.max_pages, math.ceil(limit / self.t.api.page_size)))

    def _store(self, tweets: list[Tweet], users: list[User]) -> None:
        if self._archive is None or (not tweets and not users):
            return

        async def run() -> None:
            try:
                await self._archive(tweets, users)
            except Exception as e:  # archiving must never fail a request
                log.warning("archive failed", extra={"fields": {"error": str(e)}})

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    @staticmethod
    def _tweets(result: Result, limit: int) -> tuple[list[Tweet], list[Tweet], str | None]:
        items: list[Tweet] = []
        includes: dict[int, Tweet] = {}
        seen: set[int] = set()
        cursor = None
        for page in result.pages:
            if not isinstance(page, TweetPage):  # 200 without data
                continue
            for t in page.items:
                if t.id not in seen:
                    seen.add(t.id)
                    items.append(t)
            for t in page.includes:
                includes.setdefault(t.id, t)
            cursor = page.cursor_bottom
        return items[:limit], list(includes.values()), cursor

    async def search_tweets(
        self,
        q: str,
        tab: ops.SearchTab,
        limit: int,
        consumer: str,
        cursor: str | None = None,
        max_age_sec: float | None = None,
    ) -> Envelope:
        ops.search(q, tab)  # validate before touching the gateway
        result = await self.gateway.run(
            lambda cur: ops.search(q, tab, cursor=cur),
            pages=self.pages_for(limit),
            priority=Priority.P0,
            consumer=consumer,
            cursor=cursor,
            max_age_sec=max_age_sec,
        )
        items, includes, next_cursor = self._tweets(result, limit)
        if not result.meta.cached:
            self._store(items + includes, [])
        return Envelope(items, result.meta, next_cursor, includes)

    async def search_users(
        self, q: str, limit: int, consumer: str, cursor: str | None = None, max_age_sec: float | None = None
    ) -> Envelope:
        ops.search(q, "people")
        result = await self.gateway.run(
            lambda cur: ops.search(q, "people", cursor=cur),
            pages=self.pages_for(limit),
            priority=Priority.P0,
            consumer=consumer,
            cursor=cursor,
            max_age_sec=max_age_sec,
        )
        users: list[User] = []
        seen: set[int] = set()
        next_cursor = None
        for page in result.pages:
            if not isinstance(page, UserPage):
                continue
            for u in page.items:
                if u.id not in seen:
                    seen.add(u.id)
                    users.append(u)
            next_cursor = page.cursor_bottom
        if not result.meta.cached:
            self._store([], users)
        return Envelope(users[:limit], result.meta, next_cursor)

    async def user(self, username: str, consumer: str, max_age_sec: float | None = None) -> Envelope:
        result = await self.gateway.run(
            lambda _cur: ops.user_by_screen_name(username),
            priority=Priority.P0,
            consumer=consumer,
            max_age_sec=max_age_sec,
        )
        user = result.pages[0] if result.pages else None
        if user is None:
            raise NotFound(f"user {username} not found")
        if not result.meta.cached:
            self._store([], [user])
        return Envelope(user, result.meta)

    async def user_tweets(
        self,
        username: str,
        limit: int,
        consumer: str,
        cursor: str | None = None,
        max_age_sec: float | None = None,
    ) -> Envelope:
        profile = await self.user(username, consumer, max_age_sec=PROFILE_MAX_AGE_SEC)
        uid = profile.data.id
        result = await self.gateway.run(
            lambda cur: ops.user_tweets(uid, cursor=cur),
            pages=self.pages_for(limit),
            priority=Priority.P0,
            consumer=consumer,
            cursor=cursor,
            max_age_sec=max_age_sec,
        )
        items, includes, next_cursor = self._tweets(result, limit)
        if not result.meta.cached:
            self._store(items + includes, [])
        return Envelope(items, result.meta, next_cursor, includes)
