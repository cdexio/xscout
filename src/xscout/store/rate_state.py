"""Persistence of pool quota state so a restart keeps cooldowns and remaining counts (plan 3.1)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xscout.pool.pool import BucketState
from xscout.store.models import RateLimitState


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, UTC) if ts else None


def _ts(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt else None


class RateStateRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def load(self) -> dict[int, dict[tuple[str, str], BucketState]]:
        out: dict[int, dict[tuple[str, str], BucketState]] = {}
        async with self._sessions() as s:
            for r in (await s.scalars(select(RateLimitState))).all():
                out.setdefault(r.account_id, {})[(r.operation, r.bucket)] = BucketState(
                    limit=r.limit,
                    remaining=r.remaining,
                    reset_at=_ts(r.reset_at),
                    cooling_until=_ts(r.cooling_until) or 0.0,
                    last_used_at=_ts(r.last_used_at) or 0.0,
                )
        return out

    async def upsert(self, rows: list[tuple[int, str, str, BucketState]]) -> None:
        if not rows:
            return
        values = [
            {
                "account_id": acc_id,
                "operation": op,
                "bucket": bucket,
                "limit": b.limit,
                "remaining": b.remaining,
                "reset_at": _dt(b.reset_at),
                "cooling_until": _dt(b.cooling_until or None),
                "last_used_at": _dt(b.last_used_at or None),
                "updated_at": datetime.now(UTC),
            }
            for acc_id, op, bucket, b in rows
        ]
        stmt = insert(RateLimitState).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "operation", "bucket"],
            set_={
                k: stmt.excluded[k]
                for k in ("limit", "remaining", "reset_at", "cooling_until", "last_used_at", "updated_at")
            },
        )
        async with self._sessions.begin() as s:
            await s.execute(stmt)
