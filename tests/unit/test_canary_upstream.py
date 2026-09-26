import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from xscout.canary.canary import Canary
from xscout.canary.upstream import STATE_KEY, check, check_since_last
from xscout.gateway.gateway import Meta, Result, Unavailable
from xscout.transport.session import RawResponse
from xscout.xweb import ops
from xscout.xweb.client import AccountClient
from xscout.xweb.constants import Bucket
from xscout.xweb.parse import parse_tweet_timeline, parse_user_result
from xscout.xweb.registry import OperationRegistry
from xscout.xweb.tid import TidGenerator

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "x"
PROFILE = parse_user_result(json.loads((FIX / "user_by_screen_name.json").read_text()))
GOOD = parse_tweet_timeline(json.loads((FIX / "search_latest_post.json").read_text()))


def broken_schema_page():
    """Simulated X schema change: authors lost their fields."""
    page = GOOD.model_copy(deep=True)
    for t in page.items:
        t.author = None
    page.stats.key_field_nulls = {"author": len(page.items), "author_followers": len(page.items)}
    return page


class Gw:
    """Serves canned outcomes per operation; a value may be an exception to raise."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    async def run(self, make_call, *, priority=None, consumer=None, max_age_sec=None, **kw):
        op = make_call(None).operation
        self.calls.append((op, priority.value, consumer, max_age_sec))
        value = self.outcomes[op]
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return Result([value], Meta())


class Kv:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value


def make(outcomes, heal_log=None):
    heal_log = heal_log if heal_log is not None else []

    async def heal(ops_):
        heal_log.append(ops_)

    kv = Kv()
    return Canary(Gw(outcomes), heal, history=kv, clock=lambda: 1000.0), heal_log, kv


async def test_all_ok_uses_p2_and_no_cache():
    canary, heals, _ = make({"UserByScreenName": PROFILE, "SearchTimeline": GOOD, "UserTweets": GOOD})
    report = await canary.run_once()
    assert {s["status"] for s in report.values()} == {"ok"} and heals == []
    assert all(p == "P2" and c == "canary" and age == 0 for _, p, c, age in canary.gateway.calls)
    assert canary.worst == "ok"


@pytest.mark.parametrize(
    "failure",
    [
        Unavailable("all attempts failed (not_found_op: 404)", 30),  # stale queryId
        Unavailable("all attempts failed (not_found_op: 404 after TID rebuild)", 30),  # TID rejected
    ],
)
async def test_simulated_404_breaks_and_heals_once(failure):
    canary, heals, kv = make({"UserByScreenName": PROFILE, "SearchTimeline": failure, "UserTweets": GOOD})
    report = await canary.run_once()
    assert heals == [["SearchTimeline"]]  # healed once, then re-probed without healing again
    assert report["SearchTimeline"]["status"] == "broken"
    assert len(canary.gateway.calls) == 6  # two full rounds
    history = kv.data["canary:history"]
    assert any(h["operation"] == "SearchTimeline" and h["to"] == "broken" for h in history)


async def test_simulated_schema_change_breaks():
    canary, heals, _ = make({"UserByScreenName": PROFILE, "SearchTimeline": broken_schema_page, "UserTweets": GOOD})
    report = await canary.run_once()
    assert report["SearchTimeline"]["status"] == "broken" and "null" in report["SearchTimeline"]["reason"]
    assert heals == [["SearchTimeline"]]


async def test_recovery_after_heal():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        return Unavailable("all attempts failed (not_found_op: 404)", 30) if state["n"] == 1 else GOOD

    canary, heals, _ = make({"UserByScreenName": PROFILE, "SearchTimeline": flaky, "UserTweets": GOOD})
    report = await canary.run_once()
    assert heals == [["SearchTimeline"]] and report["SearchTimeline"]["status"] == "ok"


async def test_quota_shortage_is_skipped_not_broken():
    busy = Unavailable("no quota headroom in the pool", 60)
    canary, heals, _ = make({"UserByScreenName": busy, "SearchTimeline": busy, "UserTweets": busy})
    report = await canary.run_once()
    assert {s["status"] for s in report.values()} == {"skipped"} and heals == []


async def test_degraded_on_partial_nulls():
    page = GOOD.model_copy(deep=True)
    page.stats.key_field_nulls = {"author_followers": 6}  # 30%
    canary, heals, _ = make({"UserByScreenName": PROFILE, "SearchTimeline": page, "UserTweets": GOOD})
    report = await canary.run_once()
    assert report["SearchTimeline"]["status"] == "degraded" and heals == []


# MARK: upstream


def commit(sha, date, msg):
    return {
        "sha": sha,
        "html_url": f"https://github.com/x/y/commit/{sha}",
        "commit": {"message": msg + "\n\nbody", "committer": {"date": date}},
    }


async def test_upstream_merges_paths_per_commit_and_sorts():
    calls = []

    async def fetch(url, params):
        calls.append((url, params["path"]))
        if params["path"] == "twscrape/xclid.py":
            return 200, [commit("a1", "2026-09-20T00:00:00Z", "fix xclid for new chunk hashes")]
        if params["path"] == "twscrape/api.py":
            return 200, [
                commit("a1", "2026-09-20T00:00:00Z", "fix xclid for new chunk hashes"),
                commit("b2", "2026-09-22T00:00:00Z", "update gql ops"),
            ]
        return 200, []

    changes, errors = await check(datetime(2026, 9, 1, tzinfo=UTC), fetch)
    assert errors == [] and [c.sha for c in changes] == ["b2", "a1"]
    assert changes[1].paths == ("twscrape/api.py", "twscrape/xclid.py")
    assert changes[1].message == "fix xclid for new chunk hashes"
    assert all("api.github.com/repos/" in u for u, _ in calls)


async def test_upstream_cursor_only_advances_without_errors():
    kv = Kv()

    async def failing(url, params):
        return 403, {"message": "API rate limit exceeded"}

    report = await check_since_last(kv, fetch=failing)
    assert report["errors"] and STATE_KEY not in kv.data

    async def ok(url, params):
        return 200, []

    report = await check_since_last(kv, fetch=ok)
    assert report["errors"] == [] and kv.data[STATE_KEY] == report["checked_at"]


# MARK: null-rate spike samples


class FakeTransport:
    account_id = 1

    def __init__(self, body):
        self.body = body

    async def send(self, req):
        return RawResponse(200, {"content-type": "application/json"}, self.body, 3, req.url)


class FakeTids:
    async def get(self, *a, **kw):
        return TidGenerator(list(range(48)), "k", "generator")

    def invalidate(self, account_id):
        pass


async def test_client_samples_null_spikes_once_per_interval():
    body = json.loads((FIX / "search_latest_post.json").read_text())
    for d in _walk(body):
        if isinstance(d.get("user_results"), dict):
            d["user_results"] = {}  # every author disappears
    samples = []

    async def sink(**kw):
        samples.append(kw["reason"])

    client = AccountClient(FakeTransport(json.dumps(body)), OperationRegistry(), FakeTids(), samples=sink)
    for _ in range(2):
        out = await client.call(ops.search("x"), Bucket.parse("POST/main"))
        assert out.ok
    assert len(samples) == 1 and samples[0].startswith("null-rate spike")


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)
