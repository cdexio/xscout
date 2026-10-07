import asyncio
import random
from collections import Counter
from datetime import UTC, datetime

import pytest

from xscout.cache.cache import ResultCache, cache_key
from xscout.config import BucketTunables, BudgetTunables, PoolTunables
from xscout.pool.budget import Budget, Priority
from xscout.pool.pool import AccountPool, AccountState, Lease, NoLease
from xscout.store.models import AccountStatus
from xscout.xweb.errors import RateInfo


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_pool(n: int, clock: Clock, **pool_kw) -> AccountPool:
    cfg = PoolTunables(gap_min_sec=2, gap_max_sec=2, **pool_kw)
    pool = AccountPool(cfg, BucketTunables(), clock, rng=random.Random(1))
    for i in range(1, n + 1):
        pool.upsert_account(AccountState(id=i, username=f"acc{i}"))
    return pool


def drain(pool: AccountPool, clock: Clock, op: str, n: int) -> list[Lease]:
    leases = []
    for _ in range(n):
        lease = pool.lease(op)
        if isinstance(lease, NoLease):
            clock.t += 0.5
            continue
        leases.append(lease)
        pool.release(lease)
    return leases


# MARK: pool


def test_load_spreads_evenly_over_accounts():
    clock = Clock()
    pool = make_pool(5, clock)
    leases = []
    while len(leases) < 100:
        lease = pool.lease("SearchTimeline")
        if isinstance(lease, NoLease):
            clock.t += 0.5
            continue
        leases.append(lease)
        pool.release(lease)
    per_account = Counter(lease.account_id for lease in leases)
    assert max(per_account.values()) - min(per_account.values()) <= 1
    assert {lease.bucket.name for lease in leases} == {"POST/main"}  # primary bucket has the most headroom


def test_reserve_is_never_used_and_overflow_comes_last():
    clock = Clock()
    pool = make_pool(1, clock)
    acc = pool.accounts[1]
    for name, rem in (("POST/main", 19), ("GET/main", 6)):
        b = acc.bucket("SearchTimeline", name)
        b.limit, b.remaining = (187 if name == "POST/main" else 50), rem
    buckets = []
    for _ in range(10):
        clock.t += 3
        lease = pool.lease("SearchTimeline")
        assert not isinstance(lease, NoLease)
        buckets.append(lease.bucket.name)
        pool.release(lease)
    # POST/main reserve = 18 -> 1 usable; GET/main reserve = 5 -> 1 usable; then overflow buckets
    assert buckets[:2] == ["POST/main", "GET/main"]
    assert set(buckets[2:]) <= {"POST/alt", "GET/alt"}
    assert acc.bucket("SearchTimeline", "POST/main").remaining == 18


def test_overflow_can_be_disabled_per_account():
    clock = Clock()
    pool = make_pool(1, clock)
    acc = pool.accounts[1]
    acc.allow_overflow = False
    for name in ("POST/main", "GET/main"):
        acc.bucket("SearchTimeline", name).remaining = 0
        acc.bucket("SearchTimeline", name).reset_at = clock.t + 300
    res = pool.lease("SearchTimeline")
    assert isinstance(res, NoLease) and res.retry_at == clock.t + 300


def test_window_rollover_restores_quota():
    clock = Clock()
    pool = make_pool(1, clock)
    b = pool.accounts[1].bucket("UserByScreenName", "GET/main")
    b.limit, b.remaining, b.reset_at = 150, 3, clock.t + 60
    assert pool.headroom(pool.accounts[1], "UserByScreenName", "GET/main", clock.t) == 0
    clock.t += 61
    assert pool.headroom(pool.accounts[1], "UserByScreenName", "GET/main", clock.t) == 150 - 15


def test_release_applies_headers_and_cooldowns():
    clock = Clock()
    pool = make_pool(2, clock)
    lease = pool.lease("SearchTimeline")
    reset = datetime.fromtimestamp(clock.t + 400, UTC)
    pool.release(lease, RateInfo(187, 150, reset), account_cooldown=3600)
    acc = pool.accounts[lease.account_id]
    assert acc.bucket("SearchTimeline", "POST/main").remaining == 150
    assert not pool.usable(acc, clock.t)
    other = pool.lease("SearchTimeline")
    assert not isinstance(other, NoLease) and other.account_id != lease.account_id


def test_gap_and_in_flight_block_reuse():
    clock = Clock()
    pool = make_pool(1, clock)
    first = pool.lease("SearchTimeline")
    assert isinstance(pool.lease("SearchTimeline"), NoLease)  # in flight
    pool.release(first)
    blocked = pool.lease("SearchTimeline")
    assert isinstance(blocked, NoLease) and blocked.retry_at == pytest.approx(clock.t + 2)
    clock.t += 2.1
    assert not isinstance(pool.lease("SearchTimeline"), NoLease)


def test_inactive_accounts_are_skipped_and_capacity_counts_headroom():
    clock = Clock()
    pool = make_pool(3, clock)
    pool.set_status(2, AccountStatus.LOCKED)
    per_account = (187 - 18) + (50 - 5) + (187 - 18) + (50 - 5)
    assert pool.capacity("SearchTimeline") == 2 * per_account
    assert {lease.account_id for lease in drain(pool, clock, "SearchTimeline", 10)} == {1, 3}


def test_strikes_window():
    clock = Clock()
    pool = make_pool(1, clock)
    assert pool.add_strike(1, 100) == 1
    clock.t += 50
    assert pool.add_strike(1, 100) == 2
    clock.t += 60
    assert pool.add_strike(1, 100) == 2  # the first one expired


# MARK: budget


def test_budget_shares_and_borrowing():
    clock = Clock()
    b = Budget(BudgetTunables(p0_share=0.4, p1_share=0.6), clock)
    op = "SearchTimeline"
    for _ in range(40):
        assert b.admit(op, Priority.P0, 100).admitted
        b.record(op, Priority.P0, "zetryn")
    refused = b.admit(op, Priority.P0, 60)
    assert not refused.admitted and refused.retry_after_sec > 0
    for _ in range(60):
        assert b.admit(op, Priority.P1, 100 - 40).admitted
        b.record(op, Priority.P1, "scheduler")
    assert not b.admit(op, Priority.P1, 0).admitted
    assert b.usage_by_consumer() == {"zetryn": {op: 40}, "scheduler": {op: 60}}


def test_p1_borrows_unused_p0_but_p2_leaves_it_free():
    clock = Clock()
    b = Budget(BudgetTunables(), clock)
    op = "SearchTimeline"
    admitted = 0
    for _ in range(100):
        if b.admit(op, Priority.P1, 100 - admitted).admitted:
            b.record(op, Priority.P1, "s")
            admitted += 1
    assert admitted == 100  # all of P1's share plus P0's unused share
    b2 = Budget(BudgetTunables(), clock)
    p2 = 0
    for _ in range(100):
        if b2.admit(op, Priority.P2, 100 - p2).admitted:
            b2.record(op, Priority.P2, "canary")
            p2 += 1
    assert p2 == 40  # P0's unused share (60 of 100) stays free


def test_budget_window_expires():
    clock = Clock()
    b = Budget(BudgetTunables(), clock)
    for _ in range(4):
        b.record("UserTweets", Priority.P0, "c")
    assert sum(b.used("UserTweets").values()) == 4
    clock.t += 901
    assert sum(b.used("UserTweets").values()) == 0


# MARK: cache


def test_cache_key_normalizes_query_and_screen_name():
    a = cache_key("SearchTimeline", {"rawQuery": " $BTC   news ", "count": 20}, 1)
    assert a == cache_key("SearchTimeline", {"count": 20, "rawQuery": "$BTC news"}, 1)
    assert a != cache_key("SearchTimeline", {"count": 20, "rawQuery": "$BTC news"}, 2)
    assert cache_key("U", {"screen_name": "ElonMusk"}, 1) == cache_key("U", {"screen_name": "elonmusk"}, 1)


def test_cache_ttl_max_age_stale_and_lru():
    clock = Clock()
    c = ResultCache(clock, max_entries=2)
    c.put("a", 1, ttl_sec=60)
    clock.t += 30
    assert c.get_fresh("a").value == 1
    assert c.get_fresh("a", max_age_sec=10) is None
    clock.t += 31
    assert c.get_fresh("a") is None and c.get_stale("a").value == 1
    c.put("b", 2, 60)
    c.put("c", 3, 60)
    assert c.get_stale("a") is None  # evicted


def test_cache_drops_entries_past_the_stale_limit():
    clock = Clock()
    c = ResultCache(clock, max_entries=100, stale_max_sec=3600, prune_every_sec=60)
    c.put("old", 1, ttl_sec=60)
    clock.t += 3000
    c.put("mid", 2, ttl_sec=60)
    clock.t += 601  # "old" is now 3601 s old, "mid" 601 s
    assert c.get_stale("old") is None and c.get_stale("mid").value == 2  # never served past the limit
    c.put("new", 3, ttl_sec=60)  # the minute has passed: this put prunes
    assert c.stats()["entries"] == 2 and c.stats()["pruned"] == 1
    clock.t += 10
    c.put("again", 4, ttl_sec=60)  # within the minute: no prune pass
    assert c.stats()["pruned"] == 1
    clock.t += 3601
    assert c.prune() == 3 and c.stats()["entries"] == 0


async def test_coalescing_runs_producer_once():
    c = ResultCache(Clock())
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "v"

    results = await asyncio.gather(*(c.coalesce("k", produce) for _ in range(5)))
    assert calls == 1 and [r[0] for r in results] == ["v"] * 5 and sum(r[1] for r in results) == 4
