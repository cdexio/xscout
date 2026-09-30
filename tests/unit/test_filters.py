from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from xscout.api.watch import WatchService
from xscout.config import Tunables
from xscout.scheduler.filters import WatchFilter, has_contract, passes, passing_tags
from xscout.scheduler.scheduler import FeedSignal, WatchScheduler
from xscout.scheduler.units import WatchValueError, WatchView
from xscout.xweb.models import Tweet, TweetPage, User

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SOL_CA = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
EVM_CA = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"


def tw(text="gm", **kw) -> Tweet:
    return Tweet(id=kw.pop("id", 1), text=text, author=User(id=1, username="ansem"), created_at=NOW, **kw)


def test_contract_detection():
    assert has_contract(f"new launch {SOL_CA} lfg")
    assert has_contract(f"eth one {EVM_CA}")
    assert has_contract(f"https://pump.fun/coin/{SOL_CA}")
    assert not has_contract("this is a normal sentence with nothing inside it at all ok")
    assert not has_contract("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")  # base58-shaped but not an address


@pytest.mark.parametrize(
    "tweet,spec,ok",
    [
        (tw("gm"), WatchFilter(), True),
        (tw("gm", is_reply=True), WatchFilter(exclude_replies=True), False),
        (tw("rt", is_retweet=True), WatchFilter(exclude_retweets=True), False),
        (tw("buying $WIF here"), WatchFilter(require_cashtag=True), True),
        (tw("buying here"), WatchFilter(require_cashtag=True), False),
        (tw("x", cashtags=["BONK"]), WatchFilter(require_cashtag=True), True),
        (tw(f"ca: {SOL_CA}"), WatchFilter(require_contract=True), True),
        (tw("link", urls=[f"https://dexscreener.com/solana/{SOL_CA}"]), WatchFilter(require_contract=True), True),
        (tw("no address"), WatchFilter(require_contract=True), False),
        (tw("Pump.fun is back"), WatchFilter(keywords=["PUMP.FUN", "raydium"]), True),
        (tw("nothing"), WatchFilter(keywords=["pump.fun"]), False),
    ],
)
def test_passes(tweet, spec, ok):
    assert passes(tweet, spec) is ok


def test_keyword_validation():
    assert WatchFilter(keywords=[" Pump ", "pump"]).keywords == ["pump"]
    with pytest.raises(ValidationError):
        WatchFilter(keywords=[""])
    assert WatchFilter().is_noop() and not WatchFilter(exclude_replies=True).is_noop()


def test_passing_tags_are_per_tag():
    filters = {"zetryn": WatchFilter(require_contract=True).model_dump()}
    assert passing_tags(tw("gm"), ["cdexio", "zetryn"], filters) == ["cdexio"]
    assert passing_tags(tw(f"{SOL_CA}"), ["cdexio", "zetryn"], filters) == ["cdexio", "zetryn"]


# MARK: scheduler integration


class Watches:
    def __init__(self, views):
        self.views = views

    async def enabled_views(self):
        return self.views

    async def apply_runs(self, updates):
        pass


class Feed:
    def __init__(self):
        self.entries = []

    async def append(self, entries):
        self.entries.extend(entries)
        return len(entries)


class Gw:
    def __init__(self, page):
        self.page = page

    async def run(self, make_call, **kw):
        from xscout.gateway.gateway import Meta, Result

        return Result([self.page], Meta())


async def test_scheduler_writes_only_passing_tags_and_drops_filtered():
    view = WatchView(
        id=1,
        kind="user",
        value="ansem",
        interval_sec=60,
        tags=["cdexio", "zetryn"],
        last_seen_tweet_id=0,
        created_at=NOW - timedelta(hours=1),
        filters={"zetryn": WatchFilter(require_contract=True, exclude_replies=True).model_dump()},
    )
    page = TweetPage(items=[tw("gm", id=10), tw(f"ape {SOL_CA}", id=11), tw(f"{SOL_CA}", id=12, is_reply=True)])
    feed = Feed()
    s = WatchScheduler(Gw(page), Watches([view]), feed, None, Tunables(), FeedSignal(), clock=lambda: NOW.timestamp())
    await s.reload()
    await s.poll(s.units[0])
    assert [(e.tweet_id, e.tags) for e in feed.entries] == [
        (10, ["cdexio"]),
        (11, ["cdexio", "zetryn"]),
        (12, ["cdexio"]),
    ]

    only_zetryn = WatchView(**{**view.__dict__, "tags": ["zetryn"], "last_seen_tweet_id": 0})
    feed2 = Feed()
    s2 = WatchScheduler(Gw(page), Watches([only_zetryn]), feed2, None, Tunables(), FeedSignal(), clock=lambda: 0)
    await s2.reload()
    await s2.poll(s2.units[0])
    assert [e.tweet_id for e in feed2.entries] == [11] and s2.stats["filtered"] == 2


# MARK: service validation


class MemWatches:
    def __init__(self):
        self.calls = []

    async def enabled_views(self):
        return []

    async def upsert(self, kind, value, interval, tags, consumer, filters=None):
        self.calls.append(filters)
        return {"id": 1, "filters": filters}, True


async def test_service_attaches_filter_to_request_tags():
    repo = MemWatches()
    svc = WatchService(repo, None, FeedSignal(), Tunables(), window_capacity=lambda: 10_000)
    await svc.create("user", "ansem", 60, ["zetryn-kol"], "zetryn", WatchFilter(require_cashtag=True))
    assert repo.calls[-1] == {"zetryn-kol": WatchFilter(require_cashtag=True).model_dump()}
    await svc.create("user", "ansem", 60, ["x"], "zetryn", WatchFilter())
    assert repo.calls[-1] is None  # a no-op filter is not stored
    with pytest.raises(WatchValueError):
        await svc.create("user", "ansem", 60, [], "zetryn", WatchFilter(exclude_replies=True))
