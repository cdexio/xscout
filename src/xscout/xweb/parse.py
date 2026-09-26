"""Tolerant parsers from X GraphQL responses to xscout models (spec §6, plan 2.4).

Every field is looked up in the known locations of the current and the older schema; a missing
field becomes None instead of raising. Entries that look like content but fail are counted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from xscout.xweb.models import Media, ParseStats, Tweet, TweetPage, User, UserPage

SKIP_ENTRY_PREFIXES = ("promoted-", "who-to-follow", "relevanceprompt", "messageprompt", "module-", "cursor-")
UNAVAILABLE_TYPES = {"TweetTombstone", "TweetUnavailable", "UserUnavailable"}
TWEET_KEY_FIELDS = ("text", "created_at", "author", "author_followers")


def dig(obj: Any, path: str) -> Any:
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
            cur = cur[int(key)]
        else:
            return None
        if cur is None:
            return None
    return cur


def first(obj: Any, *paths: str) -> Any:
    for p in paths:
        v = dig(obj, p)
        if v is not None:
            return v
    return None


def to_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except TypeError, ValueError:
        return None


def to_dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.strptime(v, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


# MARK: users


def parse_user(res: Any) -> User | None:
    if not isinstance(res, dict) or res.get("__typename") in UNAVAILABLE_TYPES:
        return None
    uid = to_int(first(res, "rest_id", "legacy.id_str"))
    if uid is None:
        return None
    username = first(res, "core.screen_name", "legacy.screen_name")
    return User(
        id=uid,
        username=username,
        display_name=first(res, "core.name", "legacy.name"),
        bio=first(res, "profile_bio.description", "legacy.description"),
        created_at=to_dt(first(res, "core.created_at", "legacy.created_at")),
        followers=to_int(first(res, "relationship_counts.followers", "legacy.followers_count")),
        following=to_int(first(res, "relationship_counts.following", "legacy.friends_count")),
        tweet_count=to_int(first(res, "tweet_counts.tweets", "legacy.statuses_count")),
        verified_type=first(res, "verification.verified_type", "legacy.verified_type"),
        is_blue_verified=first(res, "is_blue_verified"),
        protected=first(res, "privacy.protected", "legacy.protected"),
        location=first(res, "location.location", "legacy.location") or None,
        avatar_url=first(res, "avatar.image_url", "legacy.profile_image_url_https"),
        url=f"https://x.com/{username}" if username else None,
    )


def parse_user_result(body: Any) -> User | None:
    """UserByScreenName / UserByRestId response -> User, or None when the user does not exist."""
    return parse_user(dig(body, "data.user.result"))


# MARK: tweets


def unwrap_tweet(res: Any) -> dict | None:
    if not isinstance(res, dict):
        return None
    if res.get("__typename") == "TweetWithVisibilityResults":
        res = res.get("tweet")
    if not isinstance(res, dict) or res.get("__typename") in UNAVAILABLE_TYPES:
        return None
    return res


def _media(leg: dict) -> list[Media]:
    out: list[Media] = []
    for m in first(leg, "extended_entities.media", "entities.media") or []:
        if not isinstance(m, dict) or not m.get("media_url_https"):
            continue
        video = None
        variants = [v for v in dig(m, "video_info.variants") or [] if v.get("content_type") == "video/mp4"]
        if variants:
            video = max(variants, key=lambda v: v.get("bitrate") or 0).get("url")
        out.append(Media(type=m.get("type") or "photo", url=m["media_url_https"], video_url=video))
    return out


def parse_tweet(res: Any, includes: list[Tweet] | None = None) -> Tweet | None:
    t = unwrap_tweet(res)
    if t is None:
        return None
    leg = t.get("legacy") if isinstance(t.get("legacy"), dict) else {}
    tid = to_int(first(t, "rest_id", "legacy.id_str"))
    if tid is None:
        return None
    author = parse_user(dig(t, "core.user_results.result"))
    entities = leg.get("entities") or {}
    note = dig(t, "note_tweet.note_tweet_results.result")
    note_entities = (note or {}).get("entity_set") or {}

    def tags(key: str, field: str) -> list[str]:
        items = note_entities.get(key) or entities.get(key) or []
        return [i[field] for i in items if isinstance(i, dict) and i.get(field)]

    rt_res = unwrap_tweet(dig(leg, "retweeted_status_result.result"))
    qt_res = unwrap_tweet(first(t, "quoted_status_result.result", "legacy.quoted_status_result.result"))
    if includes is not None:
        for nested in (rt_res, qt_res):
            if nested is not None and (parsed := parse_tweet(nested)) is not None:
                includes.append(parsed)
    username = author.username if author else None
    return Tweet(
        id=tid,
        url=f"https://x.com/{username or 'i'}/status/{tid}",
        text=first(note, "text") if note else leg.get("full_text"),
        created_at=to_dt(leg.get("created_at")),
        lang=leg.get("lang"),
        author=author,
        reply_count=to_int(leg.get("reply_count")),
        retweet_count=to_int(leg.get("retweet_count")),
        quote_count=to_int(leg.get("quote_count")),
        like_count=to_int(leg.get("favorite_count")),
        view_count=to_int(dig(t, "views.count")),
        bookmark_count=to_int(leg.get("bookmark_count")),
        is_reply=leg.get("in_reply_to_status_id_str") is not None,
        is_retweet=rt_res is not None,
        is_quote=bool(leg.get("is_quote_status")),
        in_reply_to_id=to_int(leg.get("in_reply_to_status_id_str")),
        quoted_id=to_int(first(leg, "quoted_status_id_str")) or (to_int(qt_res.get("rest_id")) if qt_res else None),
        retweeted_id=to_int(rt_res.get("rest_id")) if rt_res else None,
        conversation_id=to_int(leg.get("conversation_id_str")),
        hashtags=tags("hashtags", "text"),
        cashtags=tags("symbols", "text"),
        mentions=tags("user_mentions", "screen_name"),
        urls=[u for u in (i.get("expanded_url") for i in entities.get("urls") or [] if isinstance(i, dict)) if u],
        media=_media(leg),
    )


# MARK: timelines


def _instructions(body: Any) -> list[dict]:
    """Find the timeline instructions list wherever the operation puts it."""
    for path in (
        "data.search_by_raw_query.search_timeline.timeline.instructions",
        "data.user.result.timeline.timeline.instructions",
        "data.user.result.timeline_v2.timeline.instructions",
        "data.threaded_conversation_with_injections_v2.instructions",
    ):
        found = dig(body, path)
        if isinstance(found, list):
            return found
    return []


def _entries(body: Any) -> list[tuple[dict, bool]]:
    """(entry, pinned) pairs from AddEntries, PinEntry and ReplaceEntry instructions."""
    out: list[tuple[dict, bool]] = []
    for ins in _instructions(body):
        kind = ins.get("type")
        if kind == "TimelineAddEntries":
            out.extend((e, False) for e in ins.get("entries") or [] if isinstance(e, dict))
        elif kind == "TimelinePinEntry" and isinstance(ins.get("entry"), dict):
            out.append((ins["entry"], True))
        elif kind == "TimelineReplaceEntry" and isinstance(ins.get("entry"), dict):
            out.append((ins["entry"], False))
    return out


def _cursor(entry: dict) -> tuple[str, str] | None:
    content = entry.get("content") or {}
    kind = content.get("cursorType") or dig(content, "itemContent.cursorType")
    value = content.get("value") or dig(content, "itemContent.value")
    return (kind, value) if kind and isinstance(value, str) else None


def _item_contents(entry: dict) -> list[dict]:
    content = entry.get("content") or {}
    if isinstance(content.get("itemContent"), dict):
        return [content["itemContent"]]
    return [
        dig(i, "item.itemContent") for i in content.get("items") or [] if isinstance(dig(i, "item.itemContent"), dict)
    ]


def _count_nulls(stats: ParseStats, tweet: Tweet) -> None:
    values = {
        "text": tweet.text,
        "created_at": tweet.created_at,
        "author": tweet.author,
        "author_followers": tweet.author.followers if tweet.author else None,
    }
    for k in TWEET_KEY_FIELDS:
        if values[k] is None:
            stats.key_field_nulls[k] = stats.key_field_nulls.get(k, 0) + 1


def parse_tweet_timeline(body: Any) -> TweetPage:
    page = TweetPage()
    stats = page.stats
    seen: set[int] = set()
    for entry, pinned in _entries(body):
        entry_id = str(entry.get("entryId", ""))
        if (cur := _cursor(entry)) is not None:
            if cur[0] == "Bottom":
                page.cursor_bottom = cur[1]
            elif cur[0] == "Top":
                page.cursor_top = cur[1]
            continue
        stats.entries += 1
        if entry_id.startswith(SKIP_ENTRY_PREFIXES) or "-user-" in entry_id:
            stats.skipped += 1
            continue
        for ic in _item_contents(entry):
            if ic.get("itemType") != "TimelineTweet" or ic.get("promotedMetadata"):
                stats.skipped += 1
                continue
            res = dig(ic, "tweet_results.result")
            try:
                tweet = parse_tweet(res, page.includes)
            except Exception as e:  # tolerant by design: count and continue
                stats.failed += 1
                stats.errors.append(f"{entry_id}: {type(e).__name__}: {e}"[:300])
                continue
            if tweet is None:
                stats.skipped += 1
                continue
            if tweet.id in seen:
                continue
            seen.add(tweet.id)
            tweet.is_pinned = pinned
            page.items.append(tweet)
            stats.parsed += 1
            _count_nulls(stats, tweet)
    return page


def parse_user_timeline(body: Any) -> UserPage:
    page = UserPage()
    stats = page.stats
    for entry, _ in _entries(body):
        if (cur := _cursor(entry)) is not None:
            if cur[0] == "Bottom":
                page.cursor_bottom = cur[1]
            elif cur[0] == "Top":
                page.cursor_top = cur[1]
            continue
        stats.entries += 1
        for ic in _item_contents(entry):
            if ic.get("itemType") != "TimelineUser":
                stats.skipped += 1
                continue
            try:
                user = parse_user(dig(ic, "user_results.result"))
            except Exception as e:
                stats.failed += 1
                stats.errors.append(f"{entry.get('entryId')}: {type(e).__name__}: {e}"[:300])
                continue
            if user is None:
                stats.skipped += 1
                continue
            page.items.append(user)
            stats.parsed += 1
    return page
