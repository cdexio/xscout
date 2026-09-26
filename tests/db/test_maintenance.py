from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from xscout.config import RetentionTunables
from xscout.store.maintenance import KvRepository, RetentionRepository, SampleReader
from xscout.store.models import RequestLog, ResponseSample, Tweet
from xscout.store.request_log import RequestLogRepository, RequestLogRow
from xscout.store.samples import SampleRepository
from xscout.store.tweets import TweetRepository
from xscout.xweb.models import Tweet as TweetModel
from xscout.xweb.models import User

pytestmark = pytest.mark.db


async def test_kv_set_get_overwrite(sessions):
    kv = KvRepository(sessions)
    assert await kv.get("k") is None
    await kv.set("k", {"a": 1})
    await kv.set("k", [1, 2])
    assert await kv.get("k") == [1, 2]


async def test_retention_prunes_only_old_rows(sessions):
    now = datetime.now(UTC)
    author = User(id=1, username="a")
    await TweetRepository(sessions).upsert([TweetModel(id=1, author=author), TweetModel(id=2, author=author)])
    samples = SampleRepository(sessions)
    old_sample = await samples.add("SearchTimeline", "old", "{}")
    await samples.add("SearchTimeline", "new", "{}")
    await RequestLogRepository(sessions).add_many(
        [RequestLogRow(now, "c", "P0", "SearchTimeline", "POST/main", None, 200, None, 5, "ok")] * 2
    )
    async with sessions.begin() as s:
        await s.execute(update(Tweet).where(Tweet.id == 1).values(fetched_at=now - timedelta(days=91)))
        await s.execute(
            update(ResponseSample).where(ResponseSample.id == old_sample).values(created_at=now - timedelta(days=8))
        )
        first_log = await s.scalar(select(func.min(RequestLog.id)))
        await s.execute(update(RequestLog).where(RequestLog.id == first_log).values(at=now - timedelta(days=31)))
    removed = await RetentionRepository(sessions).prune(RetentionTunables())
    assert (removed["tweets"], removed["response_samples"], removed["request_log"]) == (1, 1, 1)
    recent = await SampleReader(sessions).recent()
    assert [r["reason"] for r in recent] == ["new"]
    assert await SampleReader(sessions).body(recent[0]["id"]) == "{}"
