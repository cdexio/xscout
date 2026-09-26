"""Compact log of every request sent to X (per consumer, operation, bucket, account)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xscout.store.models import RequestLog


@dataclass(frozen=True)
class RequestLogRow:
    at: datetime
    consumer: str | None
    priority: str | None
    operation: str
    bucket: str | None
    account_id: int | None
    status: int | None
    x_error_codes: list[int] | None
    latency_ms: int | None
    outcome: str | None


class RequestLogRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def add_many(self, rows: list[RequestLogRow]) -> None:
        if not rows:
            return
        async with self._sessions.begin() as s:
            await s.execute(insert(RequestLog), [asdict(r) for r in rows])
