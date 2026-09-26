from datetime import UTC, datetime, timedelta

import pytest

from xscout.store.watch import FeedRepository, NewEntry, RunUpdate, WatchRepository

pytestmark = pytest.mark.db


async def test_upsert_merges_and_detach_deletes_last(sessions):
    repo = WatchRepository(sessions)
    a, created = await repo.upsert("user", "alice", 120, ["kol"], "zetryn")
    b, created2 = await repo.upsert("user", "alice", 60, ["tier1"], "cdexio")
    assert created and not created2 and a["id"] == b["id"]
    assert (b["interval_sec"], b["tags"], b["consumers"]) == (60, ["kol", "tier1"], ["cdexio", "zetryn"])
    assert [i["value"] for i in await repo.list(consumer="zetryn")] == ["alice"]
    assert await repo.list(tag="nope") == []
    assert await repo.detach(a["id"], "zetryn") == "detached"
    assert await repo.detach(a["id"], "cdexio") == "deleted"
    assert await repo.get(a["id"]) is None


async def test_patch_and_runs(sessions):
    repo = WatchRepository(sessions)
    item, _ = await repo.upsert("query", "$BTC", 60, [], "c")
    patched = await repo.patch(item["id"], 300, ["macro"], False)
    assert (patched["interval_sec"], patched["tags"], patched["enabled"]) == (300, ["macro"], False)
    assert await repo.enabled_views() == []
    await repo.patch(item["id"], None, None, True)
    t = datetime.now(UTC)
    await repo.apply_runs([RunUpdate(item["id"], t + timedelta(minutes=5), t, 1234, None)])
    await repo.apply_runs([RunUpdate(item["id"], t + timedelta(minutes=6), None, 1000, "busy")])  # never lowers
    [v] = await repo.enabled_views()
    assert v.last_seen_tweet_id == 1234 and v.next_run_at > t
    assert (await repo.get(item["id"]))["last_error"] == "busy"


async def test_feed_append_dedupes_and_filters_by_tags(sessions):
    watches = WatchRepository(sessions)
    feed = FeedRepository(sessions)
    a, _ = await watches.upsert("user", "alice", 60, ["kol"], "c")
    b, _ = await watches.upsert("query", "$SOL", 60, ["memecoin"], "c")
    entries = [
        NewEntry(1, a["id"], ["kol"], {"id": 1}),
        NewEntry(2, b["id"], ["memecoin"], {"id": 2}),
        NewEntry(1, a["id"], ["kol"], {"id": 1}),
    ]
    assert await feed.append(entries) == 2
    assert await feed.append(entries[:1]) == 0
    all_rows = await feed.since(0, None, 10)
    assert [r["tweet"]["id"] for r in all_rows] == [1, 2]
    assert [r["tweet"]["id"] for r in await feed.since(0, ["memecoin", "x"], 10)] == [2]
    assert await feed.since(all_rows[-1]["seq"], None, 10) == []
    assert await feed.last_seq() == all_rows[-1]["seq"]
