"""Operation registry persistence: one row per (name, query id), with history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xscout.store.models import Operation


@dataclass(frozen=True)
class OperationRecord:
    name: str
    query_id: str
    features: dict[str, object]
    feature_switches: list[str] | None
    field_toggles: list[str] | None
    source: str  # discovery | fallback | manual
    method: str | None = None


class OperationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def save_active(self, records: list[OperationRecord]) -> list[str]:
        """Upsert records as the active definition of their operation. Returns names whose query id changed."""
        changed: list[str] = []
        now = datetime.now(UTC)
        async with self._sessions.begin() as s:
            for r in records:
                prev = await s.scalar(select(Operation.query_id).where(Operation.name == r.name, Operation.active))
                if prev is not None and prev != r.query_id:
                    changed.append(r.name)
                await s.execute(update(Operation).where(Operation.name == r.name).values(active=False))
                stmt = insert(Operation).values(
                    name=r.name,
                    query_id=r.query_id,
                    features=r.features,
                    feature_switches=r.feature_switches,
                    field_toggles=r.field_toggles,
                    source=r.source,
                    method=r.method,
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["name", "query_id"],
                    set_={
                        "features": stmt.excluded.features,
                        "feature_switches": stmt.excluded.feature_switches,
                        "field_toggles": stmt.excluded.field_toggles,
                        "source": stmt.excluded.source,
                        "active": True,
                        "last_seen_at": now,
                    },
                )
                await s.execute(stmt)
        return changed

    async def active(self) -> dict[str, OperationRecord]:
        async with self._sessions() as s:
            rows = (await s.scalars(select(Operation).where(Operation.active))).all()
            return {
                r.name: OperationRecord(
                    name=r.name,
                    query_id=r.query_id,
                    features=dict(r.features or {}),
                    feature_switches=list(r.feature_switches) if r.feature_switches else None,
                    field_toggles=list(r.field_toggles) if r.field_toggles else None,
                    source=r.source,
                    method=r.method,
                )
                for r in rows
            }

    async def patch_features(self, name: str, query_id: str, features: dict[str, object]) -> None:
        async with self._sessions.begin() as s:
            await s.execute(
                update(Operation)
                .where(Operation.name == name, Operation.query_id == query_id)
                .values(features=features)
            )

    async def history(self, name: str) -> list[tuple[str, datetime, datetime, bool]]:
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(Operation.query_id, Operation.first_seen_at, Operation.last_seen_at, Operation.active)
                    .where(Operation.name == name)
                    .order_by(Operation.first_seen_at)
                )
            ).all()
            return [tuple(r) for r in rows]
