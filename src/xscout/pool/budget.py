"""Priority budget over a rolling 15-minute window (spec §5, plan 3.4).

P0 on-demand gets its share; P1 watchlist gets its share and may borrow what P0 leaves unused
(never the reverse); P2 (backfill, canary) only uses what nobody else needs. Capacity comes from the
pool's live headroom, so the budget never admits more than the accounts can actually serve.
"""

from __future__ import annotations

import enum
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from xscout.config import BudgetTunables

WINDOW_SEC = 900.0


class Priority(enum.StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


@dataclass(frozen=True)
class Admission:
    admitted: bool
    retry_after_sec: int = 0
    reason: str = ""


class Budget:
    def __init__(self, cfg: BudgetTunables, clock: Callable[[], float]):
        self.cfg = cfg
        self.clock = clock
        self._events: dict[str, deque[tuple[float, Priority, str]]] = {}  # operation -> (time, priority, consumer)

    def _window(self, operation: str) -> deque[tuple[float, Priority, str]]:
        q = self._events.setdefault(operation, deque())
        cutoff = self.clock() - WINDOW_SEC
        while q and q[0][0] < cutoff:
            q.popleft()
        return q

    def used(self, operation: str) -> dict[Priority, int]:
        counts = dict.fromkeys(Priority, 0)
        for _, p, _ in self._window(operation):
            counts[p] += 1
        return counts

    def admit(self, operation: str, priority: Priority, capacity_left: int) -> Admission:
        """capacity_left: the pool's remaining headroom for this operation right now."""
        q = self._window(operation)
        used = self.used(operation)
        total_used = sum(used.values())
        # Window capacity = what we already spent in this window + what the pool can still serve.
        capacity = total_used + max(0, capacity_left)
        if capacity_left <= 0:
            return Admission(False, self._retry_after(q), "no quota headroom in the pool")
        p0_cap = capacity * self.cfg.p0_share
        p1_cap = capacity * self.cfg.p1_share
        if priority is Priority.P0:
            ok = used[Priority.P0] < p0_cap
        elif priority is Priority.P1:
            ok = used[Priority.P1] < p1_cap + max(0.0, p0_cap - used[Priority.P0])
        else:
            ok = total_used < capacity - max(0.0, p0_cap - used[Priority.P0])  # keep P0's unused share free
        if ok:
            return Admission(True)
        return Admission(False, self._retry_after(q), f"{priority} share used up for {operation}")

    def record(self, operation: str, priority: Priority, consumer: str) -> None:
        self._window(operation).append((self.clock(), priority, consumer))

    def _retry_after(self, q: deque[tuple[float, Priority, str]]) -> int:
        if not q:
            return 60
        return max(1, int(q[0][0] + WINDOW_SEC - self.clock()) + 1)

    def usage_by_consumer(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for op in list(self._events):
            for _, _, consumer in self._window(op):
                row = out.setdefault(consumer, {})
                row[op] = row.get(op, 0) + 1
        return out
