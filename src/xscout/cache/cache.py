"""Result cache with stale serving and in-flight coalescing (spec §5, plan 3.5)."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


def cache_key(operation: str, variables: dict[str, Any], pages: int) -> str:
    norm = dict(variables)
    if isinstance(norm.get("rawQuery"), str):
        norm["rawQuery"] = " ".join(norm["rawQuery"].split())
    if isinstance(norm.get("screen_name"), str):
        norm["screen_name"] = norm["screen_name"].lower()
    return f"{operation}|{pages}|{json.dumps(norm, sort_keys=True, separators=(',', ':'))}"


@dataclass
class Entry:
    value: Any
    fetched_at: float
    expires_at: float


class ResultCache:
    """LRU by count, and every entry dropped once it is older than `stale_max_sec`.

    Without the age limit the cache kept up to `max_entries` full result pages forever (they are only
    worth serving stale for a short while), and the service grew to ~1.4 GB in 6 days on the VPS
    (2026-10-07), where it was swapped out and stopped answering under memory pressure.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        max_entries: int = 1500,
        stale_max_sec: float = 3600,
        prune_every_sec: float = 60,
    ):
        self.clock = clock
        self.max_entries = max_entries
        self.stale_max_sec = stale_max_sec
        self.prune_every_sec = prune_every_sec
        self._data: OrderedDict[str, Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._last_prune = clock()
        self.hits = self.misses = self.coalesced = self.stale_served = self.pruned = 0

    def get_fresh(self, key: str, max_age_sec: float | None = None) -> Entry | None:
        e = self._data.get(key)
        if e is None:
            return None
        now = self.clock()
        fresh = now < e.expires_at if max_age_sec is None else now - e.fetched_at <= max_age_sec
        if not fresh:
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return e

    def get_stale(self, key: str) -> Entry | None:
        e = self._data.get(key)
        if e is None or self.clock() - e.fetched_at > self.stale_max_sec:
            return None
        self.stale_served += 1
        return e

    def put(self, key: str, value: Any, ttl_sec: float) -> Entry:
        now = self.clock()
        e = Entry(value, now, now + ttl_sec)
        self._data[key] = e
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)
        if now - self._last_prune >= self.prune_every_sec:
            self.prune(now)
        return e

    def prune(self, now: float | None = None) -> int:
        """Drop every entry older than `stale_max_sec`. Returns how many were dropped."""
        now = self.clock() if now is None else now
        self._last_prune = now
        old = [k for k, e in self._data.items() if now - e.fetched_at > self.stale_max_sec]
        for k in old:
            del self._data[k]
        self.pruned += len(old)
        return len(old)

    async def coalesce(self, key: str, produce: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
        """Run `produce` once per key at a time; concurrent callers share its result. Returns (value, shared)."""
        fut = self._inflight.get(key)
        if fut is not None:
            self.coalesced += 1
            return await asyncio.shield(fut), True
        self.misses += 1
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await produce()
            fut.set_result(value)
            return value, False
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved so an unshared failure does not warn
            raise
        finally:
            self._inflight.pop(key, None)

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "coalesced": self.coalesced,
            "stale_served": self.stale_served,
            "pruned": self.pruned,
        }
