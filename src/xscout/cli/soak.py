"""`xscout soak`: load shaped like the three bots against a running xscout (plan 6.5).

Sends on-demand requests at a steady rate, keeps a watchlist near a target share of the P1
capacity, snapshots /health every minute and writes a JSON report (every 5 minutes and at the end)
with per-consumer outcomes, latency, per-account X request spread and the canary timeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from curl_cffi.requests import AsyncSession

from xscout.config import PROJECT_DIR, get_settings

CONSUMER_WATCH = "soak"
PROFILES = [
    # consumer, weight, max_age_sec, queries
    ("zetryn", 0.5, 60, ["$WIF", "$BONK", "$PEPE", "$POPCAT", "$BOME", "$FWOG", "$MEW", "$SOL", "pump.fun", "$TRUMP"]),
    ("cdexio", 0.3, 300, ["$BTC", "$ETH", "$SOL", "$XRP", "$DOGE", "bitcoin etf", "fed rate", "liquidation"]),
    ("stocks", 0.2, 600, ["$AAPL", "$TSLA", "$NVDA", "$MSFT", "$AMZN", "earnings", "$SPY", "nasdaq"]),
]
# A compact word list reads better than 30 one-item lines, hence the noqa.
KOLS = """
    elonmusk saylor VitalikButerin cz_binance WatcherGuru whale_alert DeItaone tier10k APompliano CoinDesk
    Cointelegraph BitcoinMagazine unusual_whales zerohedge KobeissiLetter lookonchain EmberCN ai_9684xtpa
    MustStopMurad blknoiz06 notthreadguy cobie HsakaTrades CryptoCapo_ rektcapital PeterLBrandt CarlRunefelt
    TheRoaringKitty jimcramer Reuters
""".split()  # noqa: SIM905


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


class Soak:
    def __init__(self, base: str, rpm: float, watch_share: float, out: Path):
        self.base = base.rstrip("/")
        self.rpm = rpm
        self.watch_share = watch_share
        self.out = out
        self.started = datetime.now(UTC)
        self.results: dict[str, list[dict]] = defaultdict(list)
        self.timeline: list[dict] = []
        self.watch_ids: list[int] = []

    async def call(self, s: AsyncSession, method: str, path: str, consumer: str, **kw) -> tuple[int, Any, float]:
        t0 = time.monotonic()
        rep = await s.request(method, self.base + path, headers={"X-Consumer": consumer}, timeout=45, **kw)
        ms = (time.monotonic() - t0) * 1000
        try:
            body = rep.json()
        except ValueError:
            body = None
        return rep.status_code, body, ms

    async def one_request(self, s: AsyncSession) -> None:
        consumer, _, max_age, queries = random.choices(PROFILES, weights=[p[1] for p in PROFILES])[0]
        if random.random() < 0.15:
            path, params = f"/v1/users/{random.choice(KOLS)}", {"max_age_sec": str(max_age * 10)}
        else:
            path = "/v1/search/tweets"
            params = {"q": random.choice(queries), "limit": "20", "max_age_sec": str(max_age)}
        try:
            status, body, ms = await self.call(s, "GET", path, consumer, params=params)
        except Exception as e:
            self.results[consumer].append({"status": 0, "ms": None, "error": str(e)[:200]})
            return
        meta = (body or {}).get("meta", {}) if isinstance(body, dict) else {}
        self.results[consumer].append(
            {"status": status, "ms": ms, "cached": bool(meta.get("cached")), "stale": bool(meta.get("stale"))}
        )

    async def setup_watchlist(self, s: AsyncSession) -> None:
        _, cap, _ = await self.call(s, "GET", "/v1/watchlist/capacity", CONSUMER_WATCH)
        target = cap["p1_capacity_per_15m"] * self.watch_share
        for i, user in enumerate(KOLS):
            _, now, _ = await self.call(s, "GET", "/v1/watchlist/capacity", CONSUMER_WATCH)
            if now["needed_per_15m"] >= target:
                break
            body = {"kind": "user", "value": user, "interval_sec": 60 if i < 15 else 120, "tags": ["soak-kol"]}
            status, item, _ = await self.call(s, "POST", "/v1/watchlist", CONSUMER_WATCH, json=body)
            if status in (200, 201):
                self.watch_ids.append(item["data"]["id"])
            elif status == 409:
                break
        for q in ("$BTC", "$SOL", "$NVDA"):
            body = {"kind": "query", "value": q, "interval_sec": 120, "tags": ["soak-query"]}
            status, item, _ = await self.call(s, "POST", "/v1/watchlist", CONSUMER_WATCH, json=body)
            if status in (200, 201):
                self.watch_ids.append(item["data"]["id"])
        _, final, _ = await self.call(s, "GET", "/v1/watchlist/capacity", CONSUMER_WATCH)
        click.echo(f"# watchlist: {len(self.watch_ids)} items, {final}", err=True)

    async def cleanup(self, s: AsyncSession) -> None:
        for item_id in self.watch_ids:
            with contextlib.suppress(Exception):  # best effort; items are listed in the report anyway
                await self.call(s, "DELETE", f"/v1/watchlist/{item_id}", CONSUMER_WATCH)

    async def snapshot(self, s: AsyncSession) -> None:
        try:
            _, h, _ = await self.call(s, "GET", "/health", CONSUMER_WATCH)
            _, accs, _ = await self.call(s, "GET", "/v1/accounts", CONSUMER_WATCH)
        except Exception as e:
            self.timeline.append({"at": datetime.now(UTC).isoformat(), "error": str(e)[:200]})
            return
        self.timeline.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "status": h.get("status"),
                "problems": h.get("problems"),
                "usable": h.get("accounts", {}).get("usable"),
                "capacity": h.get("capacity"),
                "canary": {op: st.get("status") for op, st in (h.get("canary") or {}).items()},
                "scheduler": h.get("scheduler"),
                "statuses": Counter(a.get("status") for a in accs),
            }
        )

    async def account_spread(self) -> dict[str, Any]:
        from sqlalchemy import text

        from xscout.store.db import make_engine

        engine = make_engine(get_settings().database_url)
        try:
            async with engine.connect() as c:
                rows = (
                    await c.execute(
                        text(
                            "select a.username, r.bucket, r.outcome, count(*) from request_log r "
                            "left join accounts a on a.id = r.account_id where r.at >= :t "
                            "group by 1, 2, 3 order by 1, 2, 3"
                        ),
                        {"t": self.started},
                    )
                ).all()
                statuses = (await c.execute(text("select username, status, status_reason from accounts"))).all()
        finally:
            await engine.dispose()
        per_account: dict[str, int] = Counter()
        for user, _, _, n in rows:
            per_account[user or "?"] += n
        return {
            "requests_per_account": dict(per_account),
            "spread_max_minus_min": (max(per_account.values()) - min(per_account.values())) if per_account else 0,
            "by_account_bucket_outcome": [list(r) for r in rows],
            "account_statuses": [list(r) for r in statuses],
        }

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for consumer, rs in self.results.items():
            codes = Counter(r["status"] for r in rs)
            fresh_ms = [r["ms"] for r in rs if r.get("ms") is not None and r["status"] == 200 and not r["cached"]]
            out[consumer] = {
                "requests": len(rs),
                "status_codes": dict(codes),
                "rate_503": round(codes.get(503, 0) / len(rs), 4) if rs else 0,
                "cached_ratio": round(sum(r.get("cached", False) for r in rs) / len(rs), 3) if rs else 0,
                "stale": sum(r.get("stale", False) for r in rs),
                "latency_ms_uncached_p50": pct(fresh_ms, 0.5),
                "latency_ms_uncached_p95": pct(fresh_ms, 0.95),
                "latency_ms_mean": round(statistics.fmean(fresh_ms), 1) if fresh_ms else None,
            }
        return out

    async def write(self, final: bool) -> None:
        report = {
            "started": self.started.isoformat(),
            "written": datetime.now(UTC).isoformat(),
            "final": final,
            "rpm": self.rpm,
            "consumers": self.summary(),
            "accounts": await self.account_spread(),
            "timeline": self.timeline,
        }
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(json.dumps(report, indent=2, default=str) + "\n")

    async def run(self, minutes: float) -> None:
        deadline = time.monotonic() + minutes * 60
        async with AsyncSession() as s:
            await self.setup_watchlist(s)
            tasks: set[asyncio.Task] = set()
            next_snapshot = next_write = time.monotonic()
            try:
                while time.monotonic() < deadline:
                    task = asyncio.create_task(self.one_request(s))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    now = time.monotonic()
                    if now >= next_snapshot:
                        await self.snapshot(s)
                        next_snapshot = now + 60
                    if now >= next_write:
                        await self.write(final=False)
                        next_write = now + 300
                    await asyncio.sleep(random.expovariate(self.rpm / 60.0))
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await self.cleanup(s)
                await self.snapshot(s)
                await self.write(final=True)


@click.command("soak")
@click.option("--minutes", default=1440.0, show_default=True, type=float)
@click.option("--rpm", default=20.0, show_default=True, type=float, help="On-demand requests per minute.")
@click.option("--watch-share", default=0.5, show_default=True, type=float, help="Target share of P1 capacity.")
@click.option("--base", default=None, help="xscout base URL (default from settings).")
def soak_cmd(minutes: float, rpm: float, watch_share: float, base: str | None) -> None:
    """Run a soak test against a running xscout and write logs/soak-<time>.json."""
    settings = get_settings()
    base = base or f"http://{settings.host}:{settings.port}"
    out = PROJECT_DIR / "logs" / f"soak-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    click.echo(f"# soak {minutes} min at {rpm} rpm against {base}; report: {out}", err=True)
    try:
        asyncio.run(Soak(base, rpm, watch_share, out).run(minutes))
    except KeyboardInterrupt:
        click.echo("# interrupted; partial report written", err=True)
    click.echo(str(out))
