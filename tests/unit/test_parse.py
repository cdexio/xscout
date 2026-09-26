import json
from pathlib import Path

import pytest

from xscout.xweb.parse import parse_tweet, parse_tweet_timeline, parse_user, parse_user_result, parse_user_timeline

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "x"


def load(name: str):
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.mark.parametrize("name", ["search_latest", "search_latest_post", "search_top", "search_batch_from"])
def test_search_tweet_pages(name):
    page = parse_tweet_timeline(load(name))
    assert len(page.items) == 20
    assert page.cursor_bottom and page.cursor_top
    assert page.stats.failed == 0
    for t in page.items:
        assert t.id > 0 and t.text is not None and t.created_at is not None
        assert t.author is not None and t.author.username and t.author.followers is not None
        assert t.url == f"https://x.com/{t.author.username}/status/{t.id}"
    assert page.stats.null_rate("author_followers") == 0


def test_top_skips_relevance_prompt():
    page = parse_tweet_timeline(load("search_top"))
    assert page.stats.skipped >= 1


def test_long_posts_use_note_tweet_text():
    body = load("search_latest")
    page = parse_tweet_timeline(body)
    raw = json.dumps(body)
    assert "note_tweet_results" in raw
    longest = max(page.items, key=lambda t: len(t.text or ""))
    assert len(longest.text) > 280


def test_people_search_returns_users():
    page = parse_user_timeline(load("search_people"))
    assert len(page.items) == 20 and page.cursor_bottom
    assert all(u.id and u.username and u.followers is not None for u in page.items)


def test_user_by_screen_name_new_schema():
    user = parse_user_result(load("user_by_screen_name"))
    assert user.username == "XDevelopers"
    assert user.display_name == "Developers"
    assert user.followers == 698326 and user.following == 768 and user.tweet_count == 4360
    assert user.verified_type == "Business" and user.protected is False
    assert user.location == "127.0.0.1"
    assert user.created_at.year == 2013
    assert user.avatar_url.startswith("https://pbs.twimg.com/")


def test_user_old_schema_fallback():
    old = {
        "__typename": "User",
        "rest_id": "42",
        "legacy": {
            "screen_name": "olduser",
            "name": "Old",
            "description": "bio",
            "followers_count": 10,
            "friends_count": 2,
            "statuses_count": 5,
            "created_at": "Sat Dec 14 04:35:55 +0000 2013",
            "profile_image_url_https": "https://pbs.twimg.com/a.jpg",
        },
    }
    user = parse_user(old)
    assert (user.username, user.followers, user.following, user.tweet_count, user.bio) == ("olduser", 10, 2, 5, "bio")


def test_missing_user_is_none():
    assert parse_user_result({"data": {"user": {"result": {"__typename": "UserUnavailable"}}}}) is None
    assert parse_user_result({"data": {}}) is None


def test_user_tweets_pinned_modules_and_visibility_wrapper():
    page = parse_tweet_timeline(load("user_tweets_p1"))
    assert page.stats.failed == 0
    assert sum(t.is_pinned for t in page.items) == 1
    assert len(page.items) >= 20  # 19 items + conversation module tweets + pinned, deduplicated
    assert len({t.id for t in page.items}) == len(page.items)
    assert page.stats.skipped >= 1  # who-to-follow module


def test_retweets_and_quotes_land_in_includes():
    page = parse_tweet_timeline(load("user_tweets_p2"))
    rts = [t for t in page.items if t.is_retweet]
    qts = [t for t in page.items if t.is_quote]
    assert rts and all(t.retweeted_id for t in rts)
    assert qts and all(t.quoted_id for t in qts)
    include_ids = {t.id for t in page.includes}
    assert {t.retweeted_id for t in rts} <= include_ids


def test_empty_search_has_cursors_and_no_items():
    page = parse_tweet_timeline(load("search_empty"))
    assert page.items == [] and page.cursor_bottom and page.stats.failed == 0


def test_tombstone_and_garbage_are_tolerated():
    assert parse_tweet({"__typename": "TweetTombstone"}) is None
    assert parse_tweet({"__typename": "Tweet", "rest_id": "7"}).id == 7
    assert parse_tweet_timeline({"data": None}).items == []
    body = {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": "tweet-1",
                                        "content": {
                                            "itemContent": {
                                                "itemType": "TimelineTweet",
                                                "tweet_results": {"result": {"__typename": "Tweet", "rest_id": "1"}},
                                            }
                                        },
                                    },
                                    {
                                        "entryId": "promoted-tweet-2",
                                        "content": {"itemContent": {"itemType": "TimelineTweet"}},
                                    },
                                ],
                            }
                        ]
                    }
                }
            }
        }
    }
    page = parse_tweet_timeline(body)
    assert [t.id for t in page.items] == [1]
    assert page.stats.skipped == 1
    assert page.stats.key_field_nulls["text"] == 1
