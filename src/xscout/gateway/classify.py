"""Response -> verdict: what happened, whose fault, what to do (spec §5 error table + phase 0 findings)."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from xscout.store.models import AccountStatus
from xscout.xweb.errors import Inspected


class Kind(enum.StrEnum):
    OK = "ok"
    EMPTY = "empty"  # 200 without data and without errors (e.g. unknown user)
    CALLER_ERROR = "caller_error"  # 214 etc: the request itself is invalid
    QUOTA = "quota"  # 429 / 88 with the bucket at or below zero: normal window end
    RATE_ABUSE = "rate_abuse"  # 429 / 88 while the headers say quota remains
    LOCKED = "locked"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    NOT_FOUND_OP = "not_found_op"  # 404: stale queryId / TID
    CLOUDFLARE = "cloudflare"
    TRANSIENT = "transient"  # 5xx, over capacity, load shed
    FEATURES = "features"  # 336 that self-healing could not fix
    UNKNOWN = "unknown"


class Scope(enum.StrEnum):
    NONE = "none"
    BUCKET = "bucket"  # (account, operation, bucket)
    ACCOUNT = "account"  # every operation of the account


@dataclass(frozen=True)
class Verdict:
    kind: Kind
    retry_other_account: bool = False
    cooldown_scope: Scope = Scope.NONE
    cooldown_sec: float | None = None  # None with BUCKET scope means "until the bucket's reset"
    new_status: AccountStatus | None = None
    reason: str = ""
    store_sample: bool = False
    refresh_registry: bool = False

    @property
    def success(self) -> bool:
        return self.kind in (Kind.OK, Kind.EMPTY)


LOCKED_CODES = {326}
EXPIRED_CODES = {32, 89, 239, 353}
SUSPENDED_CODES = {37, 63, 64}
CALLER_CODES = {214}
TRANSIENT_CODES = {130, 131, -1}
RATE_CODES = {88}


def _features(msg: str, cooldown: float) -> Verdict:
    """336 that the client's one-shot self-heal did not fix: cool the bucket and rediscover features."""
    return Verdict(
        kind=Kind.FEATURES,
        retry_other_account=True,
        cooldown_scope=Scope.BUCKET,
        cooldown_sec=cooldown,
        reason=msg,
        store_sample=True,
        refresh_registry=True,
    )


def classify(
    ins: Inspected,
    *,
    unknown_cooldown_sec: float,
    rate_abuse_cooldown_sec: float,
    cloudflare_cooldown_sec: float,
    transient_cooldown_sec: float,
) -> Verdict:
    codes = set(ins.codes)
    msg = "; ".join(f"{e.code}: {e.message[:160]}" for e in ins.errors)[:500]

    if codes & SUSPENDED_CODES:
        return Verdict(Kind.SUSPENDED, True, Scope.ACCOUNT, None, AccountStatus.SUSPENDED, f"suspended ({msg})")
    if codes & LOCKED_CODES:
        return Verdict(Kind.LOCKED, True, Scope.ACCOUNT, None, AccountStatus.LOCKED, f"locked ({msg})")
    if codes & EXPIRED_CODES:
        return Verdict(Kind.EXPIRED, True, Scope.ACCOUNT, None, AccountStatus.EXPIRED, f"auth failed ({msg})")
    if codes & CALLER_CODES:
        return Verdict(Kind.CALLER_ERROR, reason=msg)

    if ins.status == 429 or codes & RATE_CODES:
        remaining = ins.rate.remaining
        if remaining is not None and remaining > 0:
            return Verdict(
                Kind.RATE_ABUSE, True, Scope.ACCOUNT, rate_abuse_cooldown_sec, reason=f"429 with {remaining} left"
            )
        return Verdict(Kind.QUOTA, True, Scope.BUCKET, None, reason="quota exhausted")

    if ins.status == 200:
        if 336 in codes:
            return _features(msg, unknown_cooldown_sec)
        if ins.errors and not ins.has_data:
            return Verdict(Kind.UNKNOWN, True, Scope.BUCKET, unknown_cooldown_sec, reason=msg, store_sample=True)
        return Verdict(Kind.OK if ins.has_data else Kind.EMPTY, reason=msg)

    if ins.is_html:
        return Verdict(Kind.CLOUDFLARE, True, Scope.ACCOUNT, cloudflare_cooldown_sec, reason=f"HTML {ins.status}")
    if ins.status == 404:
        return Verdict(
            kind=Kind.NOT_FOUND_OP,
            retry_other_account=True,
            cooldown_scope=Scope.BUCKET,
            cooldown_sec=unknown_cooldown_sec,
            reason="404",
            refresh_registry=True,
        )
    if ins.status >= 500 or codes & TRANSIENT_CODES:
        return Verdict(Kind.TRANSIENT, True, Scope.BUCKET, transient_cooldown_sec, reason=f"HTTP {ins.status} {msg}")
    if ins.status == 400 and 336 in codes:
        return _features(msg, unknown_cooldown_sec)
    return Verdict(
        Kind.UNKNOWN, True, Scope.BUCKET, unknown_cooldown_sec, reason=f"HTTP {ins.status} {msg}", store_sample=True
    )
