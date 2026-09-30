import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from xscout.api.watch import OverCapacity, WatchService
from xscout.config import Tunables
from xscout.gateway.gateway import BadRequest, Meta, Result, Unavailable
from xscout.scheduler.scheduler import FeedSignal, WatchScheduler
from xscout.scheduler.units import WatchValueError, WatchView, batch_query, build_units, normalize, projected_requests
from xscout.store.watch import RunUpdate
from xscout.xweb.models import Tweet, TweetPage, User

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
T = Tunables()


def tweet(tid: int, author: str, minutes_ago: float = 1) -> Tweet:
    return Tweet(
        id=tid,
        text=f"t{tid}",
        author=User(id=hash(author) % 10**9, username=author),
        created_at=NOW - timedelta(minutes=minutes_ago),
    )


def view(i, kind="user", value="alice", interval=60, last_seen=None, created_minutes_ago=10, tags=("t",)):
    return WatchView(
        id=i,
        kind=kind,
        value=value,
        interval_sec=interval,
        tags=list(tags),
        last_seen_tweet_id=last_seen,
        created_at=NOW - timedelta(minutes=created_minutes_ago),
    )


# MARK: units


def test_normalize():
    assert normalize("user", "@Alice_1", 500) == "alice_1"
    assert normalize("query", "  $BTC   etf ", 500) == "$BTC etf"
    for kind, value in (("user", "bad name"), ("query", "  "), ("query", "x" * 501), ("other", "x")):
        with pytest.raises(WatchValueError):
            normalize(kind, value, 500)


def test_users_batch_by_interval_within_char_limit():
    users = [view(i, value=f"user{i:03d}_abcdefgh") for i in range(60)]  # 18-char names
    users += [view(100 + i, value=f"slow{i}", interval=600) for i in range(3)]
    queries = [view(200, kind="query", value="$SOL")]
    units = build_units(users + queries, 500)
    fast = [u for u in units if u.kind == "user" and u.interval_sec == 60]
    assert all(len(u.query) <= 500 for u in units)
    assert sum(len(u.items) for u in fast) == 60 and len(fast) >= 3
    assert [u.query for u in units if u.interval_sec == 600] == [batch_query(["slow0", "slow1", "slow2"])]
    assert [u.query for u in units if u.kind == "query"] == ["$SOL"]
    assert projected_requests(units) == pytest.approx(len(fast) * 15 + 1.5 + 15)


def test_disabled_items_are_not_polled():
    v = view(1)
    v.enabled = False
    assert build_units([v], 500) == []


# MARK: scheduler fakes


class FakeWatches:
    def __init__(self, views):
        self.views = views
        self.updates: list[RunUpdate] = []

    async def enabled_views(self):
        return [v for v in self.views if v.enabled]

    async def apply_runs(self, updates):
        self.updates.extend(updates)


class FakeFeed:
    def __init__(self):
        self.entries = []

    async def append(self, entries):
        seen = {(x.tweet_id, x.watch_item_id) for x in self.entries}
        new = [e for e in entries if (e.tweet_id, e.watch_item_id) not in seen]
        self.entries.extend(new)
        return len(new)


class FakeGateway:
    def __init__(self, pages=None, fail=None):
        self.pages = pages or []  # list of TweetPage served in order
        self.fail = fail
        self.calls = []

    async def run(self, make_call, *, pages=1, priority=None, consumer="?", cursor=None, max_age_sec=None):
        call = make_call(cursor)
        self.calls.append((call.operation, call.variables, priority, max_age_sec))
        if self.fail:
            raise self.fail
        if call.operation == "UserByScreenName":
            return Result([User(id=7, username=call.variables["screen_name"])], Meta())
        return Result([self.pages.pop(0) if self.pages else TweetPage()], Meta())


NOW_TS = NOW.timestamp()


def scheduler(views, gw, clock_t=NOW_TS):
    feed = FakeFeed()
    watches = FakeWatches(views)
    archived = []

    async def archive(tweets, users):
        archived.extend(tweets)

    s = WatchScheduler(gw, watches, feed, archive, T, FeedSignal(), clock=lambda: clock_t)
    return s, watches, feed, archived


async def test_first_run_emits_only_recent_tweets_and_sets_watermark():
    page = TweetPage(items=[tweet(100, "alice", 2), tweet(90, "alice", 60), tweet(95, "bob", 3)], cursor_bottom="c")
    s, watches, feed, archived = scheduler([view(1, value="alice"), view(2, value="bob")], FakeGateway([page]))
    await s.reload()
    [unit] = s.units
    res = await s.poll(unit)
    assert res.pages == 1  # first run: one page only
    assert sorted((e.watch_item_id, e.tweet_id) for e in feed.entries) == [(1, 100), (2, 95)]  # 90 is too old
    assert {u.last_seen_tweet_id for u in watches.updates} == {100}
    assert {u.next_run_at for u in watches.updates} == {NOW + timedelta(seconds=60)}
    assert len(archived) == 3


async def test_later_runs_page_until_watermark_and_skip_seen():
    p1 = TweetPage(items=[tweet(300, "alice"), tweet(250, "alice")], cursor_bottom="c1")
    p2 = TweetPage(items=[tweet(210, "alice"), tweet(190, "alice")], cursor_bottom="c2")
    gw = FakeGateway([p1, p2])
    s, _, feed, _ = scheduler([view(1, value="alice", last_seen=200)], gw)
    await s.reload()
    res = await s.poll(s.units[0])
    assert res.pages == 2 and [c[1].get("cursor") for c in gw.calls] == [None, "c1"]
    assert sorted(e.tweet_id for e in feed.entries) == [210, 250, 300]
    assert all(c[2].value == "P1" and c[3] == 0 for c in gw.calls)


async def test_unavailable_reschedules_with_retry_after():
    s, watches, feed, _ = scheduler([view(1, interval=300)], FakeGateway(fail=Unavailable("busy", 20)))
    await s.reload()
    res = await s.poll(s.units[0])
    assert res.error.startswith("unavailable") and not feed.entries
    assert watches.updates[0].next_run_at == NOW + timedelta(seconds=20)
    assert watches.updates[0].error


async def test_rejected_batch_falls_back_to_user_tweets():
    class Gw(FakeGateway):
        async def run(self, make_call, **kw):
            call = make_call(kw.get("cursor"))
            if call.operation == "SearchTimeline":
                self.calls.append((call.operation,))
                raise BadRequest("214")
            return await super().run(make_call, **kw)

    gw = Gw([TweetPage(items=[tweet(500, "alice")])])
    s, watches, feed, _ = scheduler([view(1, value="alice", last_seen=400)], gw)
    await s.reload()
    res = await s.poll(s.units[0])
    assert [c[0] for c in gw.calls] == ["SearchTimeline", "UserByScreenName", "UserTweets"]
    assert [e.tweet_id for e in feed.entries] == [500] and res.new_entries == 1


async def test_tick_runs_due_units_once():
    gw = FakeGateway([TweetPage(items=[tweet(10, "alice")])])
    s, _, _, _ = scheduler([view(1, value="alice")], gw)
    tasks = await s.tick()
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    assert await s.tick() == []  # next run is in the future now


async def test_feed_signal_wakes_waiters():
    sig = FeedSignal()
    v = sig.version
    waiter = asyncio.create_task(sig.wait(v, 1.0))
    await asyncio.sleep(0)
    await sig.bump()
    assert await waiter is True
    assert await sig.wait(sig.version, 0.01) is False


# MARK: service


class MemWatches(FakeWatches):
    def __init__(self):
        super().__init__([])
        self.rows = {}

    async def all_views(self):
        return list(self.views)

    async def upsert(self, kind, value, interval, tags, consumer):
        for v in self.views:
            if (v.kind, v.value) == (kind, value):
                v.interval_sec = min(v.interval_sec, interval)
                return {"id": v.id}, False
        v = view(len(self.views) + 1, kind=kind, value=value, interval=interval, tags=tags)
        self.views.append(v)
        return {"id": v.id}, True


def service(capacity: int):
    changes = []
    svc = WatchService(
        MemWatches(), None, FeedSignal(), T, window_capacity=lambda: capacity, on_change=lambda: changes.append(1)
    )
    return svc, changes


async def test_create_merges_and_validates():
    svc, changes = service(capacity=1000)
    item, created = await svc.create("user", "@Alice", 60, ["KOL-tier1"], "zetryn")
    assert created and changes
    _, created2 = await svc.create("user", "alice", 30, [], "cdexio")
    assert not created2 and svc.watches.views[0].interval_sec == 30
    with pytest.raises(WatchValueError):
        await svc.create("user", "alice", 5, [], "c")  # below min interval
    with pytest.raises(WatchValueError):
        await svc.create("query", "x", 60, ["Bad Tag!"], "c")


async def test_capacity_is_enforced():
    svc, _ = service(capacity=150)  # P1 share (0.4): 60 requests per 15 min
    await svc.create("query", "$BTC", 30, [], "c")  # 30 per window
    await svc.create("query", "$ETH", 30, [], "c")  # 60: exactly at capacity, still allowed
    with pytest.raises(OverCapacity) as e:
        await svc.create("query", "$SOL", 60, [], "c")  # +15 -> 75
    assert e.value.needed == pytest.approx(75) and e.value.available == pytest.approx(60)
    report = await svc.capacity_report()
    assert report == {"needed_per_15m": 60.0, "p1_capacity_per_15m": 60.0}
