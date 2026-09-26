import asyncio
import json
import random
from collections import Counter
from pathlib import Path

import pytest

from xscout.cache.cache import ResultCache
from xscout.config import Tunables
from xscout.gateway.gateway import BadRequest, Gateway, Unavailable
from xscout.pool.budget import Budget, Priority
from xscout.pool.pool import AccountPool, AccountState
from xscout.store.models import AccountStatus
from xscout.transport.session import RawResponse, TransportError
from xscout.xweb import ops
from xscout.xweb.client import CallOutcome
from xscout.xweb.errors import inspect
from xscout.xweb.requests import TidRequired

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "x"
SEARCH_BODY = (FIX / "search_latest_post.json").read_text()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class FakeClient:
    """Returns scripted (status, body, headers) per call; records (account, bucket)."""

    def __init__(self, account_id, script, log):
        self.account_id = account_id
        self.script = script
        self.log = log

    async def call(self, call, bucket):
        self.log.append((self.account_id, bucket.name))
        await asyncio.sleep(0)  # yield like a real network call
        item = self.script.pop(0) if self.script else (200, SEARCH_BODY, {})
        if isinstance(item, Exception):
            raise item
        status, body, headers = item
        rep = RawResponse(status, {"content-type": "application/json", **headers}, body, 5, "https://x.com/")
        ins = inspect(rep)
        parsed = call.parser(ins.body) if status == 200 and ins.has_data else None
        return CallOutcome(call.operation, bucket, rep, ins, parsed, "generator", 1)


class Events:
    def __init__(self):
        self.statuses = []
        self.requests = []
        self.refreshes = 0
        self.samples = 0

    async def status_changed(self, account_id, status, reason):
        self.statuses.append((account_id, status))

    async def ct0_rotated(self, account_id, ct0):
        pass

    async def sample(self, **kw):
        self.samples += 1

    async def refresh_registry(self, account_id):
        self.refreshes += 1

    def request(self, row):
        self.requests.append(row)


def build(n_accounts=3, scripts=None, gap=1.0):
    clock = Clock()
    t = Tunables.model_validate({"pool": {"gap_min_sec": gap, "gap_max_sec": gap}})
    pool = AccountPool(t.pool, t.buckets, clock, rng=random.Random(3))
    calls = []
    clients = {}
    for i in range(1, n_accounts + 1):
        pool.upsert_account(AccountState(id=i, username=f"acc{i}"))
        clients[i] = FakeClient(i, list((scripts or {}).get(i, [])), calls)

    async def sleep(sec):
        # concurrent sleepers wake at their own target time; they do not add up
        target = clock.t + sec
        await asyncio.sleep(0)
        clock.t = max(clock.t, target)

    events = Events()
    gw = Gateway(t, pool, Budget(t.budget, clock), ResultCache(clock), clients.get, events, clock=clock, sleep=sleep)
    return gw, clock, calls, events, pool


def search(q="bitcoin"):
    return lambda cur: ops.search(q, cursor=cur)


def hdr(remaining, limit=187, reset_in=600):
    return {
        "x-rate-limit-limit": str(limit),
        "x-rate-limit-remaining": str(remaining),
        "x-rate-limit-reset": str(int(1_800_000_000 + reset_in)),
    }


async def test_success_then_cache_hit():
    gw, clock, calls, events, _ = build()
    r1 = await gw.run(search(), consumer="zetryn")
    assert len(r1.pages[0].items) == 20 and not r1.meta.cached and r1.meta.attempts == 1
    r2 = await gw.run(search(), consumer="cdexio")
    assert r2.meta.cached and len(calls) == 1
    assert events.requests[0].consumer == "zetryn" and events.requests[0].outcome == "ok"


async def test_identical_concurrent_requests_share_one_call():
    gw, _, calls, _, _ = build()
    results = await asyncio.gather(*(gw.run(search("$SOL")) for _ in range(4)))
    assert len(calls) == 1 and sum(r.meta.shared for r in results) == 3


async def test_many_queries_spread_over_accounts():
    gw, _, calls, _, _ = build(n_accounts=3, gap=0.2)
    await asyncio.gather(*(gw.run(search(f"q{i}")) for i in range(30)))
    per = Counter(a for a, _ in calls)
    assert len(calls) == 30 and max(per.values()) - min(per.values()) <= 1


async def test_rate_abuse_cools_account_and_retries_elsewhere():
    err = json.dumps({"errors": [{"code": 88, "message": "Rate limit exceeded"}]})
    gw, clock, calls, _, pool = build(n_accounts=2, scripts={1: [(429, err, hdr(40))]})
    pool.accounts[2].last_used_at = 1  # acc1 (never used) is the first choice
    r = await gw.run(search())
    assert [a for a, _ in calls] == [1, 2] and r.pages[0].items
    assert pool.accounts[1].cooling_until >= clock.t + 3599
    assert pool.accounts[1].strikes and not pool.accounts[2].strikes


async def test_quota_exhausted_cools_bucket_until_reset_only():
    gw, clock, calls, _, pool = build(n_accounts=1, scripts={1: [(429, "", hdr(0, reset_in=300))]})
    r = await gw.run(search())
    b = pool.accounts[1].bucket("SearchTimeline", "POST/main")
    assert r.pages[0].items and [bk for _, bk in calls] == ["POST/main", "GET/main"]
    assert b.cooling_until == pytest.approx(1_800_000_000 + 300)
    assert pool.accounts[1].cooling_until == 0.0 and not pool.accounts[1].strikes


async def test_three_strikes_lock_the_account():
    err = json.dumps({"errors": [{"code": 88, "message": "Rate limit exceeded"}]})
    gw, clock, calls, events, pool = build(n_accounts=1, scripts={1: [(429, err, hdr(30))] * 3})
    for _ in range(3):
        with pytest.raises(Unavailable):
            await gw.run(search(f"x{clock.t}"))
        pool.accounts[1].cooling_until = 0  # let the next attempt reach X again
        clock.t += 5
    assert events.statuses == [(1, AccountStatus.LOCKED)]
    assert pool.accounts[1].status == AccountStatus.LOCKED


async def test_expired_account_is_retired_and_other_serves():
    err = json.dumps({"errors": [{"code": 32, "message": "Could not authenticate you"}]})
    gw, _, calls, events, pool = build(n_accounts=2, scripts={1: [(401, err, {})], 2: [(401, err, {})]})
    with pytest.raises(Unavailable):
        await gw.run(search())
    assert {s for _, s in events.statuses} == {AccountStatus.EXPIRED}
    assert all(pool.accounts[i].status == AccountStatus.EXPIRED for i in (1, 2))


async def test_query_error_214_is_bad_request_without_penalty():
    body = json.dumps({"errors": [{"code": 214, "message": "Raw query length exceeds max allowed 512"}], "data": {}})
    gw, _, calls, events, pool = build(n_accounts=1, scripts={1: [(200, body, hdr(100))]})
    with pytest.raises(BadRequest):
        await gw.run(search())
    assert pool.usable(pool.accounts[1], 0) and not events.statuses and len(calls) == 1


async def test_network_error_and_missing_tid_retry():
    gw, _, calls, _, _ = build(n_accounts=2, scripts={1: [TransportError("reset")], 2: [TidRequired("no tid")]})
    r = await gw.run(search())
    assert r.pages[0].items and len(calls) == 3


async def test_no_capacity_gives_unavailable_with_retry_after():
    gw, clock, _, _, pool = build(n_accounts=1)
    acc = pool.accounts[1]
    for b in ("POST/main", "GET/main", "POST/alt", "GET/alt"):
        st = acc.bucket("SearchTimeline", b)
        st.limit, st.remaining, st.reset_at = 50, 0, clock.t + 120
    with pytest.raises(Unavailable) as e:
        await gw.run(search(), priority=Priority.P1)
    assert 1 <= e.value.retry_after_sec <= 900


async def test_stale_cache_served_when_unavailable():
    gw, clock, _, _, pool = build(n_accounts=1)
    await gw.run(search())
    clock.t += 3600  # cache expired, then the account gets locked
    pool.set_status(1, AccountStatus.LOCKED)
    r = await gw.run(search())
    assert r.meta.stale and r.meta.cached and r.meta.age_sec >= 3600


async def test_multiple_pages_follow_cursor_and_keep_partial():
    gw, _, calls, _, pool = build(n_accounts=1)
    r = await gw.run(search(), pages=3)
    assert r.meta.pages_fetched == 3 and len(calls) == 3
    gw2, _, calls2, _, pool2 = build(
        n_accounts=1, scripts={1: [(200, SEARCH_BODY, {}), (503, "", {}), (503, "", {}), (503, "", {})]}
    )
    r2 = await gw2.run(search(), pages=3)
    assert r2.meta.pages_fetched == 1


async def test_404_requests_registry_refresh():
    gw, _, _, events, _ = build(n_accounts=2, scripts={1: [(404, "", {})], 2: [(404, "", {})]})
    r = await gw.run(search())
    assert events.refreshes >= 1 and r.pages
