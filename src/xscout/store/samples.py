"""Raw response samples for debugging X changes (spec §6); retention is enforced in phase 6."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xscout.log import redactor
from xscout.store.models import ResponseSample

MAX_BODY_CHARS = 2_000_000


class SampleRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def add(
        self,
        operation: str,
        reason: str,
        body: str | None,
        status: int | None = None,
        bucket: str | None = None,
        account_id: int | None = None,
    ) -> int:
        async with self._sessions.begin() as s:
            row = ResponseSample(
                operation=operation,
                bucket=bucket,
                account_id=account_id,
                status=status,
                reason=reason[:2000],
                body=redactor.redact(body[:MAX_BODY_CHARS]) if body else None,
            )
            s.add(row)
            await s.flush()
            return row.id
