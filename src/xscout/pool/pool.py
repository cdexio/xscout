"""Account pool: fair selection with quota headroom per (account, operation, bucket) (spec §5, plan 3.1-3.2).

In memory for speed; the gateway persists dirty state in the background. Times are epoch seconds
from an injectable clock so tests can run without sleeping.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

from xscout.config import BucketTunables, PoolTunables
from xscout.store.models import AccountStatus
from xscout.xweb.constants import Bucket
from xscout.xweb.errors import RateInfo

FALLBACK_LIMIT = 50  # when neither headers nor config know an operation's limit


@dataclass
class BucketState:
    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None
    cooling_until: float = 0.0
    last_used_at: float = 0.0
    dirty: bool = False


@dataclass
class AccountState:
    id: int
    username: str
    status: str = AccountStatus.ACTIVE
    allow_overflow: bool = True
    cooling_until: float = 0.0
    next_allowed_at: float = 0.0  # minimum gap between two requests of this account
    last_used_at: float = 0.0
    in_flight: int = 0
    strikes: list[float] = field(default_factory=list)  # 429-with-quota events, for the lock rule
    buckets: dict[tuple[str, str], BucketState] = field(default_factory=dict)

    def bucket(self, operation: str, bucket: str) -> BucketState:
        return self.buckets.setdefault((operation, bucket), BucketState())


@dataclass(frozen=True)
class Lease:
    account_id: int
    username: str
    operation: str
    bucket: Bucket
    leased_at: float


@dataclass(frozen=True)
class NoLease:
    retry_at: float | None  # earliest time an account may become available; None = not within a window
    reason: str


class AccountPool:
    def __init__(
        self,
        pool_cfg: PoolTunables,
        bucket_cfg: BucketTunables,
        clock: Callable[[], float],
        rng: random.Random | None = None,
    ):
        self.cfg = pool_cfg
        self.bucket_cfg = bucket_cfg
        self.clock = clock
        self.rng = rng or random.Random()
        self.accounts: dict[int, AccountState] = {}

    # MARK: membership

    def upsert_account(self, state: AccountState) -> None:
        """Add an account, or refresh its stored metadata while keeping runtime state (gaps, strikes, quota)."""
        existing = self.accounts.get(state.id)
        if existing is None:
            self.accounts[state.id] = state
            return
        existing.username = state.username
        existing.status = state.status
        existing.allow_overflow = state.allow_overflow
        existing.cooling_until = max(existing.cooling_until, state.cooling_until)
        for key, b in state.buckets.items():
            existing.buckets.setdefault(key, b)

    def remove_account(self, account_id: int) -> None:
        self.accounts.pop(account_id, None)

    # MARK: quota math

    def default_limit(self, operation: str, bucket: str) -> int:
        return self.bucket_cfg.limit_defaults.get(operation, {}).get(bucket, FALLBACK_LIMIT)

    def reserve(self, limit: int) -> int:
        return max(self.cfg.reserve_min, int(limit * self.cfg.reserve_ratio))

    def _roll(self, b: BucketState, now: float) -> None:
        if b.reset_at is not None and now >= b.reset_at:
            b.remaining = b.limit
            b.reset_at = None
            b.dirty = True

    def headroom(self, acc: AccountState, operation: str, bucket: str, now: float) -> int:
        b = acc.bucket(operation, bucket)
        self._roll(b, now)
        if b.cooling_until > now:
            return 0
        limit = b.limit if b.limit is not None else self.default_limit(operation, bucket)
        remaining = b.remaining if b.remaining is not None else limit
        return max(0, remaining - self.reserve(limit))

    def buckets_for(self, operation: str, acc: AccountState) -> tuple[list[str], list[str]]:
        order = self.bucket_cfg.order.get(operation, ["GET/main"])
        overflow = set(self.bucket_cfg.overflow)
        primary = [b for b in order if b not in overflow]
        extra = [b for b in order if b in overflow] if self.bucket_cfg.overflow_enabled and acc.allow_overflow else []
        return primary, extra

    def _best_bucket(self, acc: AccountState, operation: str, now: float) -> tuple[str | None, int]:
        primary, extra = self.buckets_for(operation, acc)
        for group in (primary, extra):
            best, best_room = None, 0
            for name in group:  # earlier buckets win ties
                room = self.headroom(acc, operation, name, now)
                if room > best_room:
                    best, best_room = name, room
            if best is not None:
                return best, best_room
        return None, 0

    def usable(self, acc: AccountState, now: float) -> bool:
        return acc.status == AccountStatus.ACTIVE and acc.cooling_until <= now

    def capacity(self, operation: str) -> int:
        """Requests the pool can still make for an operation in the current windows (headroom sum)."""
        now = self.clock()
        total = 0
        for acc in self.accounts.values():
            if not self.usable(acc, now):
                continue
            primary, extra = self.buckets_for(operation, acc)
            total += sum(self.headroom(acc, operation, b, now) for b in primary + extra)
        return total

    # MARK: lease / release

    def lease(self, operation: str) -> Lease | NoLease:
        now = self.clock()
        candidates: list[tuple[int, float, float, AccountState, str]] = []
        retry_at: float | None = None
        for acc in self.accounts.values():
            if not self.usable(acc, now):
                if acc.status == AccountStatus.ACTIVE:
                    retry_at = _min(retry_at, acc.cooling_until)
                continue
            bucket, room = self._best_bucket(acc, operation, now)
            if bucket is None:
                retry_at = _min(retry_at, self._next_bucket_time(acc, operation, now))
                continue
            if acc.in_flight >= self.cfg.max_in_flight_per_account or acc.next_allowed_at > now:
                retry_at = _min(retry_at, max(acc.next_allowed_at, now + 0.25))
                continue
            candidates.append((room, -acc.last_used_at, self.rng.random(), acc, bucket))
        if not candidates:
            if not self.accounts:
                return NoLease(None, "no accounts")
            return NoLease(retry_at, "no account with quota headroom")
        candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
        _, _, _, acc, bucket = candidates[0]
        b = acc.bucket(operation, bucket)
        limit = b.limit if b.limit is not None else self.default_limit(operation, bucket)
        b.remaining = (b.remaining if b.remaining is not None else limit) - 1  # optimistic; headers correct it
        b.last_used_at = now
        b.dirty = True
        acc.in_flight += 1
        acc.last_used_at = now
        acc.next_allowed_at = now + self.rng.uniform(self.cfg.gap_min_sec, self.cfg.gap_max_sec)
        return Lease(acc.id, acc.username, operation, Bucket.parse(bucket), now)

    def _next_bucket_time(self, acc: AccountState, operation: str, now: float) -> float | None:
        primary, extra = self.buckets_for(operation, acc)
        times: list[float] = []
        for name in primary + extra:
            b = acc.bucket(operation, name)
            if b.cooling_until > now:
                times.append(b.cooling_until)
            elif b.reset_at is not None:
                times.append(b.reset_at)
        return min(times) if times else None

    def release(
        self,
        lease: Lease,
        rate: RateInfo | None = None,
        *,
        bucket_cooldown: float | None = None,
        bucket_until_reset: bool = False,
        account_cooldown: float | None = None,
    ) -> None:
        acc = self.accounts.get(lease.account_id)
        if acc is None:
            return
        now = self.clock()
        acc.in_flight = max(0, acc.in_flight - 1)
        b = acc.bucket(lease.operation, lease.bucket.name)
        if rate is not None and rate.limit is not None:
            b.limit = rate.limit
        if rate is not None and rate.remaining is not None:
            b.remaining = rate.remaining
        if rate is not None and rate.reset_at is not None:
            b.reset_at = rate.reset_at.timestamp()
        if bucket_until_reset:
            b.remaining = 0
            b.cooling_until = max(b.cooling_until, b.reset_at or now + 900)
        if bucket_cooldown:
            b.cooling_until = max(b.cooling_until, now + bucket_cooldown)
        if account_cooldown:
            acc.cooling_until = max(acc.cooling_until, now + account_cooldown)
        b.dirty = True

    def add_strike(self, account_id: int, window_sec: float) -> int:
        """Record a 429-with-quota event; returns strikes inside the window."""
        acc = self.accounts.get(account_id)
        if acc is None:
            return 0
        now = self.clock()
        acc.strikes = [t for t in acc.strikes if now - t < window_sec] + [now]
        return len(acc.strikes)

    def set_status(self, account_id: int, status: str) -> None:
        if (acc := self.accounts.get(account_id)) is not None:
            acc.status = status

    def cool_bucket(self, account_id: int, operation: str, bucket: str, seconds: float) -> None:
        if (acc := self.accounts.get(account_id)) is not None:
            b = acc.bucket(operation, bucket)
            b.cooling_until = max(b.cooling_until, self.clock() + seconds)
            b.dirty = True

    def dirty_buckets(self) -> list[tuple[int, str, str, BucketState]]:
        out = []
        for acc in self.accounts.values():
            for (op, bucket), b in acc.buckets.items():
                if b.dirty:
                    out.append((acc.id, op, bucket, b))
                    b.dirty = False
        return out

    def snapshot(self) -> list[dict]:
        now = self.clock()
        rows = []
        for acc in sorted(self.accounts.values(), key=lambda a: a.username):
            rows.append(
                {
                    "username": acc.username,
                    "status": acc.status,
                    "usable": self.usable(acc, now),
                    "cooling_for_sec": max(0, int(acc.cooling_until - now)),
                    "in_flight": acc.in_flight,
                    "buckets": {
                        f"{op} {bk}": {
                            "limit": b.limit,
                            "remaining": b.remaining,
                            "reset_in_sec": int(b.reset_at - now) if b.reset_at else None,
                            "cooling_for_sec": max(0, int(b.cooling_until - now)),
                        }
                        for (op, bk), b in sorted(acc.buckets.items())
                    },
                }
            )
        return rows


def _min(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
