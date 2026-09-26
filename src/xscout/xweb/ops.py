"""Operation variables and response parsers for the v1 operations (verified in phase 0)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from xscout.xweb.parse import parse_tweet_timeline, parse_user_result, parse_user_timeline

SearchTab = Literal["latest", "top", "people"]
PRODUCTS: dict[str, str] = {"latest": "Latest", "top": "Top", "people": "People"}
MAX_QUERY_CHARS = 512  # X answers error 214 above this (phase 0)


class QueryTooLong(ValueError):
    pass


@dataclass(frozen=True)
class OpCall:
    operation: str
    variables: dict[str, Any]
    parser: Callable[[Any], Any]
    paginated: bool


def search(query: str, tab: SearchTab = "latest", count: int = 20, cursor: str | None = None) -> OpCall:
    query = query.strip()
    if not query:
        raise ValueError("empty search query")
    if len(query) > MAX_QUERY_CHARS:
        raise QueryTooLong(f"search query is {len(query)} characters; X allows {MAX_QUERY_CHARS}")
    variables: dict[str, Any] = {
        "rawQuery": query,
        "count": count,
        "querySource": "typed_query",
        "product": PRODUCTS[tab],
    }
    if cursor:
        variables["cursor"] = cursor
    parser = parse_user_timeline if tab == "people" else parse_tweet_timeline
    return OpCall("SearchTimeline", variables, parser, paginated=True)


def user_by_screen_name(username: str) -> OpCall:
    return OpCall(
        "UserByScreenName",
        {"screen_name": username.lstrip("@"), "withSafetyModeUserFields": True},
        parse_user_result,
        paginated=False,
    )


def user_tweets(user_id: int, count: int = 20, cursor: str | None = None) -> OpCall:
    variables: dict[str, Any] = {
        "userId": str(user_id),
        "count": count,
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
    }
    if cursor:
        variables["cursor"] = cursor
    return OpCall("UserTweets", variables, parse_tweet_timeline, paginated=True)
