"""The only path to X (spec §4-§5, plan 3.3-3.6).

cache -> coalescing -> budget -> lease -> call -> classify -> release -> retry on another account.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from xscout.cache.cache import ResultCache, cache_key
from xscout.config import Tunables
from xscout.gateway.classify import Kind, Scope, Verdict, classify
from xscout.pool.budget import Budget, Priority
from xscout.pool.pool import AccountPool, Lease, NoLease
from xscout.store.models import AccountStatus
from xscout.store.request_log import RequestLogRow
from xscout.transport.session import TransportError
from xscout.xweb.client import CallOutcome
from xscout.xweb.constants import Bucket
from xscout.xweb.ops import OpCall
from xscout.xweb.requests import TidRequired

log = logging.getLogger("xscout.gateway")


class Unavailable(Exception):
    def __init__(self, reason: str, retry_after_sec: int):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_sec = max(1, int(retry_after_sec))


class BadRequest(Exception):
    pass


class Client(Protocol):
    async def call(self, call: OpCall, bucket: Bucket) -> CallOutcome: ...


class Events(Protocol):
    """Side effects the gateway reports; the runtime persists them."""

    async def status_changed(self, account_id: int, status: AccountStatus, reason: str) -> None: ...
    async def ct0_rotated(self, account_id: int, ct0: str) -> None: ...
    async def sample(
        self, operation: str, reason: str, body: str, status: int, bucket: str, account_id: int
    ) -> None: ...
    async def refresh_registry(self, account_id: int) -> None: ...
    def request(self, row: RequestLogRow) -> None: ...


class NullEvents:
    async def status_changed(self, account_id, status, reason):
        pass

    async def ct0_rotated(self, account_id, ct0):
        pass

    async def sample(self, **kw):
        pass

    async def refresh_registry(self, account_id):
        pass

    def request(self, row):
        pass


@dataclass
class Meta:
    cached: bool = False
    stale: bool = False
    shared: bool = False
    age_sec: int = 0
    fetched_at: datetime | None = None
    pages_fetched: int = 0
    attempts: int = 0


@dataclass
class Result:
    pages: list[Any]
    meta: Meta = field(default_factory=Meta)


@dataclass
class _Fetched:
    pages: list[Any]
    fetched_at: datetime
    attempts: int


class Gateway:
    def __init__(
        self,
        tunables: Tunables,
        pool: AccountPool,
        budget: Budget,
        cache: ResultCache,
        clients: Callable[[int], Client | None],
        events: Events | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.t = tunables
        self.pool = pool
        self.budget = budget
        self.cache = cache
        self.clients = clients
        self.events: Events = events or NullEvents()
        self.clock = clock
        self.sleep = sleep
        self.stats: dict[str, int] = {}
        self.last_verdict: dict[str, tuple[str, str, float]] = {}  # operation -> (kind, reason, time)

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    # MARK: public

    async def run(
        self,
        make_call: Callable[[str | None], OpCall],
        *,
        pages: int = 1,
        priority: Priority = Priority.P0,
        consumer: str = "unknown",
        cursor: str | None = None,
        max_age_sec: float | None = None,
    ) -> Result:
        first = make_call(cursor)
        key = cache_key(first.operation, first.variables, pages)
        ttl = self.t.cache.ttl_sec.get(first.operation, self.t.cache.default_ttl_sec)
        hit = self.cache.get_fresh(key, max_age_sec)
        if hit is not None:
            return self._result(hit.value, cached=True, fetched_ts=hit.fetched_at)

        async def produce() -> _Fetched:
            fetched = await self._fetch(make_call, cursor, pages, priority, consumer)
            self.cache.put(key, fetched, ttl)
            return fetched

        try:
            fetched, shared = await self.cache.coalesce(key, produce)
        except Unavailable:
            stale = self.cache.get_stale(key)
            if stale is not None and priority is Priority.P0:
                return self._result(stale.value, cached=True, stale=True, fetched_ts=stale.fetched_at)
            raise
        return self._result(fetched, shared=shared)

    def _result(
        self, f: _Fetched, *, cached=False, stale=False, shared=False, fetched_ts: float | None = None
    ) -> Result:
        age = int(self.clock() - fetched_ts) if fetched_ts is not None else 0
        return Result(
            f.pages,
            Meta(
                cached=cached,
                stale=stale,
                shared=shared,
                age_sec=max(0, age),
                fetched_at=f.fetched_at,
                pages_fetched=len(f.pages),
                attempts=0 if cached else f.attempts,
            ),
        )

    # MARK: fetching

    async def _fetch(
        self,
        make_call: Callable[[str | None], OpCall],
        cursor: str | None,
        pages: int,
        priority: Priority,
        consumer: str,
    ) -> _Fetched:
        out: list[Any] = []
        attempts_total = 0
        for i in range(pages):
            call = make_call(cursor)
            try:
                parsed, attempts = await self._one(call, priority, consumer)
            except Unavailable:
                if out:  # keep what we already have; the caller sees pages_fetched < pages
                    break
                raise
            attempts_total += attempts
            out.append(parsed)
            cursor = getattr(parsed, "cursor_bottom", None)
            if not call.paginated or not cursor or not getattr(parsed, "items", None):
                break
            if i + 1 < pages:
                self._count("pages_extra")
        return _Fetched(out, datetime.now(UTC), attempts_total)

    async def _one(self, call: OpCall, priority: Priority, consumer: str) -> tuple[Any, int]:
        op = call.operation
        max_wait = self.t.gateway.max_wait_sec_p0 if priority is Priority.P0 else self.t.gateway.max_wait_sec_p1
        deadline = self.clock() + max_wait
        attempts = 0
        last_reason = "no attempt"
        while attempts < self.t.gateway.max_attempts:
            adm = self.budget.admit(op, priority, self.pool.capacity(op))
            if not adm.admitted:
                self._count("budget_refused")
                raise Unavailable(adm.reason, adm.retry_after_sec)
            lease = self.pool.lease(op)
            if isinstance(lease, NoLease):
                now = self.clock()
                if lease.retry_at is not None and lease.retry_at <= deadline:
                    await self.sleep(max(0.05, lease.retry_at - now))
                    continue
                retry = math.ceil(lease.retry_at - now) if lease.retry_at else 60
                self._count("no_lease")
                raise Unavailable(lease.reason, retry)
            client = self.clients(lease.account_id)
            if client is None:
                self.pool.release(lease, account_cooldown=60)
                continue
            attempts += 1
            self.budget.record(op, priority, consumer)
            try:
                outcome = await client.call(call, lease.bucket)
            except TidRequired:
                self.pool.release(lease, bucket_cooldown=self.t.gateway.tid_missing_cooldown_sec)
                self._log(lease, consumer, priority, None, None, "tid_missing")
                last_reason = "no transaction id available"
                continue
            except TransportError as e:
                self.pool.release(lease)
                self._log(lease, consumer, priority, None, None, "network")
                last_reason = f"network: {e}"
                self._count("network_errors")
                await self.sleep(self.t.gateway.network_backoff_sec * attempts)
                continue
            verdict = classify(
                outcome.inspected,
                unknown_cooldown_sec=self.t.cooldowns.unknown_error_sec,
                rate_abuse_cooldown_sec=self.t.cooldowns.rate_limited_sec,
                cloudflare_cooldown_sec=self.t.cooldowns.cloudflare_sec,
                transient_cooldown_sec=self.t.gateway.transient_cooldown_sec,
            )
            await self._apply(lease, outcome, verdict)
            self._log(lease, consumer, priority, outcome, verdict, verdict.kind.value)
            self._count(f"verdict_{verdict.kind.value}")
            self.last_verdict[op] = (verdict.kind.value, verdict.reason, self.clock())
            if verdict.success:
                return outcome.parsed, attempts
            if verdict.kind is Kind.CALLER_ERROR:
                raise BadRequest(verdict.reason or "invalid request")
            last_reason = f"{verdict.kind.value}: {verdict.reason}"
            if not verdict.retry_other_account:
                break
        raise Unavailable(f"all attempts failed ({last_reason})", 30)

    async def _apply(self, lease: Lease, outcome: CallOutcome, verdict: Verdict) -> None:
        bucket_cd = verdict.cooldown_sec if verdict.cooldown_scope is Scope.BUCKET else None
        until_reset = verdict.cooldown_scope is Scope.BUCKET and verdict.cooldown_sec is None
        account_cd = verdict.cooldown_sec if verdict.cooldown_scope is Scope.ACCOUNT else None
        self.pool.release(
            lease,
            outcome.inspected.rate,
            bucket_cooldown=bucket_cd,
            bucket_until_reset=until_reset,
            account_cooldown=account_cd,
        )
        new_status = verdict.new_status
        if verdict.kind is Kind.RATE_ABUSE:
            strikes = self.pool.add_strike(lease.account_id, self.t.cooldowns.rate_limited_lock_window_sec)
            if strikes >= self.t.cooldowns.rate_limited_lock_after:
                new_status = AccountStatus.LOCKED
        if new_status is not None:
            self.pool.set_status(lease.account_id, new_status)
            log.warning(
                "account status changed",
                extra={"fields": {"account": lease.username, "status": str(new_status), "reason": verdict.reason}},
            )
            await self.events.status_changed(lease.account_id, new_status, verdict.reason)
        transport = getattr(self.clients(lease.account_id), "transport", None)
        if transport is not None and getattr(transport, "ct0_rotated", False):
            await self.events.ct0_rotated(lease.account_id, transport.ct0)  # the runtime dedupes repeats
        if verdict.store_sample:
            await self.events.sample(
                operation=lease.operation,
                reason=verdict.reason or verdict.kind.value,
                body=outcome.response.text,
                status=outcome.response.status,
                bucket=lease.bucket.name,
                account_id=lease.account_id,
            )
        if verdict.refresh_registry:
            await self.events.refresh_registry(lease.account_id)

    def _log(
        self,
        lease: Lease,
        consumer: str,
        priority: Priority,
        outcome: CallOutcome | None,
        verdict: Verdict | None,
        result: str,
    ) -> None:
        self.events.request(
            RequestLogRow(
                at=datetime.now(UTC),
                consumer=consumer,
                priority=priority.value,
                operation=lease.operation,
                bucket=lease.bucket.name,
                account_id=lease.account_id,
                status=outcome.response.status if outcome else None,
                x_error_codes=outcome.inspected.codes or None if outcome else None,
                latency_ms=outcome.response.elapsed_ms if outcome else None,
                outcome=result,
            )
        )
