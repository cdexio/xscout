import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from xscout.store.models import Tweet, User
from xscout.store.tweets import TweetRepository
from xscout.xweb.parse import parse_tweet_timeline

pytestmark = pytest.mark.db
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "x"


async def test_upsert_is_idempotent_and_updates_counts(sessions):
    page = parse_tweet_timeline(json.loads((FIX / "user_tweets_p2.json").read_text()))
    repo = TweetRepository(sessions)
    n_tweets, n_users = await repo.upsert(page.items + page.includes)
    assert n_tweets >= 20 and n_users >= 1
    first = page.items[0]
    first.like_count = (first.like_count or 0) + 1000
    await repo.upsert([first])
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Tweet)) == n_tweets
        assert await s.scalar(select(func.count()).select_from(User)) == n_users
        row = await s.get(Tweet, first.id)
        assert row.like_count == first.like_count and row.author_username == first.author.username
