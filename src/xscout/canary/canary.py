"""Known-answer probes of the xweb layer (spec §6 canary, plan 6.1).

Every run spends a few P2 requests: a profile, a busy search and that profile's tweets. Each
operation gets a state (ok / degraded / broken / skipped) with a reason. A new `broken` triggers the
heal callback (registry rediscovery + TID rebuild) and one immediate re-probe.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from xscout.gateway.gateway import BadRequest, Gateway, Unavailable
from xscout.pool.budget import Priority
from xscout.xweb import ops
from xscout.xweb.models import TweetPage, User

log = logging.getLogger("xscout.canary")

CONSUMER = "canary"
PROFILE = "XDevelopers"
SEARCH = "bitcoin"
MIN_SEARCH_ITEMS = 5
DEGRADED_NULL_RATE = 0.2
BROKEN_NULL_RATE = 0.8
QUOTA_HINTS = ("quota", "share used up", "no account", "headroom")
HISTORY_KEY = "canary:history"
HISTORY_MAX = 100


@dataclass
class ComponentState:
    status: str  # ok | degraded | broken | skipped | unknown
    reason: str = ""
    checked_at: float = 0.0
    since: float = 0.0


def _judge_tweets(page: Any) -> tuple[str, str]:
    if not isinstance(page, TweetPage):
        return "broken", "no timeline in response"
    if len(page.items) < MIN_SEARCH_ITEMS:
        return "broken", f"only {len(page.items)} tweets parsed (stats {page.stats.model_dump()})"
    rates = {f: page.stats.null_rate(f) for f in ("text", "created_at", "author", "author_followers")}
    worst_field, worst = max(rates.items(), key=lambda kv: kv[1])
    if worst >= BROKEN_NULL_RATE:
        return "broken", f"{worst_field} null in {worst:.0%} of tweets"
    if worst >= DEGRADED_NULL_RATE or page.stats.failed:
        return "degraded", f"{worst_field} null in {worst:.0%}; failed entries {page.stats.failed}"
    return "ok", f"{len(page.items)} tweets"


def _judge_user(user: Any) -> tuple[str, str]:
    if not isinstance(user, User):
        return "broken", "profile of a known account not parsed"
    if not user.username or user.followers is None:
        return "degraded", "profile parsed without username or followers"
    return "ok", f"@{user.username} {user.followers} followers"


class Canary:
    def __init__(
        self,
        gateway: Gateway,
        heal: Callable[[list[str]], Awaitable[None]],
        history: Any | None = None,  # KvRepository-like with get/set
        clock: Callable[[], float] = time.time,
    ):
        self.gateway = gateway
        self.heal = heal
        self.history = history
        self.clock = clock
        self.state: dict[str, ComponentState] = {
            op: ComponentState("unknown") for op in ("UserByScreenName", "SearchTimeline", "UserTweets")
        }

    def report(self) -> dict[str, dict]:
        return {op: asdict(s) for op, s in self.state.items()}

    @property
    def worst(self) -> str:
        order = ["broken", "degraded", "unknown", "skipped", "ok"]
        return min((s.status for s in self.state.values()), key=order.index)

    async def _probe(self, make_call, judge) -> tuple[str, str, Any]:
        try:
            result = await self.gateway.run(make_call, priority=Priority.P2, consumer=CONSUMER, max_age_sec=0)
        except Unavailable as e:
            if any(h in e.reason for h in QUOTA_HINTS):
                return "skipped", f"no quota for the canary: {e.reason}", None
            return "broken", e.reason, None
        except BadRequest as e:
            return "broken", f"bad request: {e}", None
        page = result.pages[0] if result.pages else None
        status, reason = judge(page)
        return status, reason, page

    async def run_once(self, allow_heal: bool = True) -> dict[str, dict]:
        results: dict[str, tuple[str, str]] = {}
        status, reason, user = await self._probe(lambda _c: ops.user_by_screen_name(PROFILE), _judge_user)
        results["UserByScreenName"] = (status, reason)
        status, reason, _ = await self._probe(lambda cur: ops.search(SEARCH, "latest", cursor=cur), _judge_tweets)
        results["SearchTimeline"] = (status, reason)
        if isinstance(user, User):
            uid = user.id
            status, reason, _ = await self._probe(lambda cur: ops.user_tweets(uid, cursor=cur), _judge_tweets)
            results["UserTweets"] = (status, reason)
        else:
            results["UserTweets"] = ("skipped", "profile probe failed, no user id")
        newly_broken = await self._record(results)
        if newly_broken and allow_heal:
            log.warning("canary: components broken, healing", extra={"fields": {"broken": newly_broken}})
            await self.heal(newly_broken)
            return await self.run_once(allow_heal=False)
        return self.report()

    async def _record(self, results: dict[str, tuple[str, str]]) -> list[str]:
        now = self.clock()
        changed: list[dict] = []
        newly_broken: list[str] = []
        for op, (status, reason) in results.items():
            cur = self.state[op]
            if status == "skipped" and cur.status not in ("unknown", "skipped"):
                cur.checked_at = now  # keep the last real verdict
                continue
            if status != cur.status:
                changed.append({"at": now, "operation": op, "from": cur.status, "to": status, "reason": reason})
                if status == "broken":
                    newly_broken.append(op)
                level = logging.WARNING if status in ("broken", "degraded") else logging.INFO
                log.log(level, "canary state changed", extra={"fields": changed[-1]})
                cur.since = now
            cur.status, cur.reason, cur.checked_at = status, reason, now
        if changed and self.history is not None:
            past = await self.history.get(HISTORY_KEY) or []
            await self.history.set(HISTORY_KEY, (past + changed)[-HISTORY_MAX:])
        return newly_broken
