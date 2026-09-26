"""Wires storage, accounts, xweb and the gateway together and runs the background loops.

Used by the HTTP API (phase 4) and the developer CLI. Background loops: state flush (quota state and
request log), account sync from the DB (picks up accounts added through the CLI), registry refresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from xscout.cache.cache import ResultCache
from xscout.config import Settings
from xscout.crypto import SecretBox
from xscout.gateway.gateway import Gateway
from xscout.pool.budget import Budget
from xscout.pool.pool import AccountPool, AccountState
from xscout.store.accounts import AccountCredentials, AccountRepository
from xscout.store.db import make_engine, make_sessionmaker
from xscout.store.models import AccountStatus
from xscout.store.operations import OperationRepository
from xscout.store.rate_state import RateStateRepository
from xscout.store.request_log import RequestLogRepository, RequestLogRow
from xscout.store.samples import SampleRepository
from xscout.transport.session import AccountTransport
from xscout.xweb.client import AccountClient
from xscout.xweb.pages import fetch_text
from xscout.xweb.registry import OperationRegistry
from xscout.xweb.tid import TidProvider

log = logging.getLogger("xscout.runtime")

ACCOUNT_SYNC_SEC = 60.0
CORE_OPS = ("SearchTimeline", "UserByScreenName", "UserTweets")


def _cred_key(c: AccountCredentials) -> tuple:
    return (c.auth_token, c.proxy, c.impersonate)


class Runtime:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.t = settings.tunables
        self.clock = clock
        self.engine = make_engine(settings.database_url)
        self.sessions = make_sessionmaker(self.engine)
        self.accounts = AccountRepository(self.sessions, SecretBox(settings.secret_key.get_secret_value()))
        self.operations = OperationRepository(self.sessions)
        self.rate_repo = RateStateRepository(self.sessions)
        self.request_repo = RequestLogRepository(self.sessions)
        self.samples = SampleRepository(self.sessions)
        self.registry = OperationRegistry(self.operations)
        self.tids = TidProvider(rebuild_sec=self.t.intervals.tid_rebuild_sec, clock=clock)
        self.pool = AccountPool(self.t.pool, self.t.buckets, clock)
        self.budget = Budget(self.t.budget, clock)
        self.cache = ResultCache(clock, self.t.gateway.cache_max_entries)
        self.clients: dict[int, AccountClient] = {}
        self._cred_keys: dict[int, tuple] = {}
        self._persisted_ct0: dict[int, str] = {}
        self._pending_requests: list[RequestLogRow] = []
        self._last_registry_refresh = 0.0
        self._tasks: list[asyncio.Task] = []
        self._side_tasks: set[asyncio.Task] = set()
        self.gateway = Gateway(self.t, self.pool, self.budget, self.cache, self.clients.get, events=self, clock=clock)

    # MARK: lifecycle

    async def start(self, background: bool = True) -> None:
        await self.registry.load()
        stored = await self.rate_repo.load()
        await self.sync_accounts()
        for acc_id, buckets in stored.items():
            if (acc := self.pool.accounts.get(acc_id)) is not None:
                for key, b in buckets.items():
                    acc.buckets.setdefault(key, b)
        if not await self.operations.active() and self.clients:
            await self.refresh_registry(next(iter(self.clients)))
        if background:
            self._tasks = [
                asyncio.create_task(self._loop(self.flush, self.t.gateway.state_flush_sec)),
                asyncio.create_task(self._loop(self.sync_accounts, ACCOUNT_SYNC_SEC)),
                asyncio.create_task(self._loop(self._scheduled_refresh, self.t.intervals.registry_refresh_sec)),
            ]
        counts = {"accounts": len(self.pool.accounts), "clients": len(self.clients)}
        log.info("runtime started", extra={"fields": counts})

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._side_tasks:
            await asyncio.gather(*self._side_tasks, return_exceptions=True)
        await self.flush()
        for client in self.clients.values():
            await client.transport.close()
        self.clients.clear()
        await self.engine.dispose()

    async def _loop(self, fn, every: float) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await fn()
            except Exception as e:  # a failing loop must not kill the service
                log.warning("background task failed", extra={"fields": {"task": fn.__name__, "error": str(e)}})

    # MARK: status reports (served by /health, /v1/accounts, /v1/budget)

    def health_report(self) -> dict:
        now = self.clock()
        accounts = list(self.pool.accounts.values())
        usable = [a for a in accounts if self.pool.usable(a, now) and a.id in self.clients]
        specs = self.registry.snapshot()
        tid_layers = {self.tids.layer_for(a.id) for a in usable}
        problems = []
        if self.registry.last_error:
            problems.append(f"registry: {self.registry.last_error}")
        if any(specs[n].source == "fallback" for n in CORE_OPS if n in specs):
            problems.append("registry: using hardcoded fallback query ids")
        if self.tids.last_error:
            problems.append(f"tid: {self.tids.last_error}")
        status = "down" if not usable else "degraded" if problems else "ok"
        return {
            "status": status,
            "problems": problems,
            "accounts": {
                "total": len(accounts),
                "active": sum(1 for a in accounts if a.status == AccountStatus.ACTIVE),
                "usable": len(usable),
            },
            "registry": {n: {"query_id": s.query_id, "source": s.source} for n, s in sorted(specs.items())},
            "tid_layers": sorted(tid_layers),
            "capacity": {op: self.pool.capacity(op) for op in CORE_OPS},
            "cache": self.cache.stats(),
            "gateway": dict(self.gateway.stats),
        }

    def accounts_report(self) -> list[dict]:
        return self.pool.snapshot()

    def budget_report(self) -> dict:
        return {
            "window_sec": 900,
            "shares": {"P0": self.t.budget.p0_share, "P1": self.t.budget.p1_share},
            "operations": {
                op: {
                    "capacity_left": self.pool.capacity(op),
                    "used": {p.value: n for p, n in self.budget.used(op).items()},
                }
                for op in CORE_OPS
            },
            "by_consumer": self.budget.usage_by_consumer(),
        }

    # MARK: accounts

    async def sync_accounts(self) -> None:
        views = await self.accounts.list()
        creds = {c.id: c for c in await self.accounts.active_credentials()}
        seen = set()
        for v in views:
            seen.add(v.id)
            self.pool.upsert_account(
                AccountState(
                    id=v.id,
                    username=v.username,
                    status=v.status,
                    allow_overflow=v.allow_overflow,
                    cooling_until=v.cooling_until.timestamp() if v.cooling_until else 0.0,
                )
            )
            c = creds.get(v.id)
            if c is None:
                await self._drop_client(v.id)
                continue
            if self._cred_keys.get(v.id) != _cred_key(c):
                await self._drop_client(v.id)
                transport = AccountTransport(c)
                self.clients[v.id] = AccountClient(transport, self.registry, self.tids, self.samples.add)
                self._cred_keys[v.id] = _cred_key(c)
                self._persisted_ct0[v.id] = c.ct0
                self.tids.invalidate(v.id)
        for acc_id in list(self.pool.accounts):
            if acc_id not in seen:
                self.pool.remove_account(acc_id)
                await self._drop_client(acc_id)

    async def _drop_client(self, account_id: int) -> None:
        client = self.clients.pop(account_id, None)
        self._cred_keys.pop(account_id, None)
        if client is not None:
            await client.transport.close()

    # MARK: persistence

    async def flush(self) -> None:
        await self.rate_repo.upsert(self.pool.dirty_buckets())
        rows, self._pending_requests = self._pending_requests, []
        await self.request_repo.add_many(rows)

    # MARK: gateway events

    async def status_changed(self, account_id: int, status: AccountStatus, reason: str) -> None:
        await self.accounts.set_status_by_id(account_id, status, reason)
        if status != AccountStatus.ACTIVE:
            await self._drop_client(account_id)

    async def ct0_rotated(self, account_id: int, ct0: str) -> None:
        if self._persisted_ct0.get(account_id) == ct0:
            return
        await self.accounts.update_ct0(account_id, ct0)
        self._persisted_ct0[account_id] = ct0
        log.info("ct0 rotated and stored", extra={"fields": {"account_id": account_id}})

    async def sample(self, **kw) -> None:
        await self.samples.add(**kw)

    async def refresh_registry(self, account_id: int) -> None:
        now = self.clock()
        if now - self._last_registry_refresh < self.t.gateway.registry_refresh_min_interval_sec:
            return
        client = self.clients.get(account_id) or next(iter(self.clients.values()), None)
        if client is None:
            return
        self._last_registry_refresh = now
        changed = await self.registry.refresh(lambda url: fetch_text(client.transport, url))
        if changed:
            self.tids.invalidate(client.transport.account_id)

    async def _scheduled_refresh(self) -> None:
        if self.clients:
            self._last_registry_refresh = 0.0
            await self.refresh_registry(next(iter(self.clients)))

    def request(self, row: RequestLogRow) -> None:
        self._pending_requests.append(row)
        if row.account_id is not None:
            task = asyncio.get_running_loop().create_task(self._touch(row))
            self._side_tasks.add(task)
            task.add_done_callback(self._side_tasks.discard)

    async def _touch(self, row: RequestLogRow) -> None:
        error = None if row.outcome in ("ok", "empty") else row.outcome
        acc = self.pool.accounts.get(row.account_id)
        cooling = datetime.fromtimestamp(acc.cooling_until, UTC) if acc and acc.cooling_until > self.clock() else None
        try:
            await self.accounts.record_use(row.account_id, row.at, error=error, cooling_until=cooling)
        except Exception as e:
            log.warning("record_use failed", extra={"fields": {"error": str(e)}})
