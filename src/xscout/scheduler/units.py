"""Watch items -> poll units, and the capacity math for them (spec §8, plan 5.1-5.2).

User watches with the same interval are batched into `(from:a OR from:b ...)` Latest searches of at
most `max_chars` characters (X rejects queries over 512; the 26-term, 490-character form was verified
in phase 0). Each query watch is its own unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

USERNAME_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
WINDOW_SEC = 900


class WatchValueError(ValueError):
    pass


@dataclass
class WatchView:
    """What the scheduler needs from a watch item row."""

    id: int
    kind: str  # "user" | "query"
    value: str
    interval_sec: int
    tags: list[str]
    enabled: bool = True
    next_run_at: datetime | None = None
    last_seen_tweet_id: int | None = None
    created_at: datetime | None = None
    filters: dict[str, dict] = field(default_factory=dict)  # tag -> WatchFilter fields


@dataclass
class Unit:
    key: str
    kind: str
    query: str
    interval_sec: int
    items: list[WatchView] = field(default_factory=list)

    def due(self, now: datetime) -> bool:
        return any(i.next_run_at is None or i.next_run_at <= now for i in self.items)

    def overdue_since(self, now: datetime) -> float:
        times = [(now - i.next_run_at).total_seconds() if i.next_run_at else 1e9 for i in self.items]
        return max(times) if times else 0.0


def normalize(kind: str, value: str, max_chars: int) -> str:
    if kind == "user":
        m = USERNAME_RE.fullmatch(value.strip())
        if not m:
            raise WatchValueError(f"invalid X username: {value!r}")
        return m.group(1).lower()
    if kind == "query":
        q = " ".join(value.split())
        if not q:
            raise WatchValueError("empty query")
        if len(q) > max_chars:
            raise WatchValueError(f"query is {len(q)} characters; the limit is {max_chars}")
        return q
    raise WatchValueError(f"kind must be user or query, not {kind!r}")


def batch_query(usernames: list[str]) -> str:
    return "(" + " OR ".join(f"from:{u}" for u in usernames) + ")"


def build_units(items: list[WatchView], max_chars: int) -> list[Unit]:
    units: list[Unit] = []
    by_interval: dict[int, list[WatchView]] = {}
    for item in items:
        if not item.enabled:
            continue
        if item.kind == "query":
            units.append(Unit(f"q:{item.id}", "query", item.value, item.interval_sec, [item]))
        else:
            by_interval.setdefault(item.interval_sec, []).append(item)
    for interval, group in sorted(by_interval.items()):
        group.sort(key=lambda i: i.value)
        chunk: list[WatchView] = []
        for item in group:
            if chunk and len(batch_query([c.value for c in chunk] + [item.value])) > max_chars:
                units.append(_user_unit(interval, chunk))
                chunk = []
            chunk.append(item)
        if chunk:
            units.append(_user_unit(interval, chunk))
    return units


def _user_unit(interval: int, chunk: list[WatchView]) -> Unit:
    names = [c.value for c in chunk]
    return Unit(f"u:{interval}:{names[0]}:{names[-1]}", "user", batch_query(names), interval, list(chunk))


def projected_requests(units: list[Unit], pages_per_poll: float = 1.0) -> float:
    """Expected SearchTimeline requests per 15-minute window."""
    return sum(WINDOW_SEC / u.interval_sec * pages_per_poll for u in units)
