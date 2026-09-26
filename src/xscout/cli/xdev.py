"""`xscout x ...`: single-account developer commands (plan 2.5). Not for production traffic."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import click

from xscout.config import get_settings
from xscout.crypto import SecretBox
from xscout.store.accounts import AccountRepository
from xscout.store.db import make_engine, make_sessionmaker
from xscout.store.models import AccountStatus
from xscout.store.operations import OperationRepository
from xscout.store.samples import SampleRepository
from xscout.transport.session import AccountTransport
from xscout.xweb import ops
from xscout.xweb.client import AccountClient, CallOutcome
from xscout.xweb.constants import Bucket
from xscout.xweb.pages import fetch_text
from xscout.xweb.registry import OperationRegistry
from xscout.xweb.tid import TidProvider


@dataclass
class Ctx:
    client: AccountClient
    registry: OperationRegistry


@asynccontextmanager
async def _context(account: str | None, discover: bool) -> AsyncIterator[Ctx]:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    try:
        accounts = AccountRepository(sessions, SecretBox(settings.secret_key.get_secret_value()))
        if account is None:
            active = [v for v in await accounts.list() if v.status == AccountStatus.ACTIVE]
            if not active:
                raise click.ClickException("no active account; add one with `xscout accounts add`")
            account = active[0].username
        transport = AccountTransport(await accounts.credentials(account))
        try:
            op_repo = OperationRepository(sessions)
            registry = OperationRegistry(op_repo)
            await registry.load()
            if discover or not await op_repo.active():
                changed = await registry.refresh(lambda url: fetch_text(transport, url))
                click.echo(f"# discovery: changed={changed} error={registry.last_error}", err=True)
            tids = TidProvider(rebuild_sec=settings.tunables.intervals.tid_rebuild_sec)
            client = AccountClient(transport, registry, tids, SampleRepository(sessions).add)
            yield Ctx(client, registry)
        finally:
            await transport.close()
    finally:
        await engine.dispose()


def _meta(o: CallOutcome) -> None:
    r = o.inspected.rate
    click.echo(
        f"# {o.operation} {o.bucket} status={o.response.status} rate={r.remaining}/{r.limit} "
        f"reset={r.reset_at.isoformat() if r.reset_at else None} tid={o.tid_layer} attempts={o.attempts} "
        f"errors={[(e.code, e.message[:120]) for e in o.inspected.errors]} ms={o.response.elapsed_ms}",
        err=True,
    )


def _emit(obj: Any) -> None:
    click.echo(json.dumps(obj, default=str, ensure_ascii=False, indent=2))


def _run(fn: Callable[[], Awaitable[None]]) -> None:
    try:
        asyncio.run(fn())
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f"{type(e).__name__}: {e}") from e


@click.group("x")
def x() -> None:
    """Single-account developer calls to X (use sparingly; they spend real quota)."""


account_opt = click.option("--account", default=None, help="Account username (default: first active).")
discover_opt = click.option("--discover", is_flag=True, help="Run queryId discovery before the call.")


def _bucket(value: str) -> Bucket:
    try:
        return Bucket.parse(value)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


@x.command("discover")
@account_opt
def x_discover(account: str | None) -> None:
    """Run queryId/feature discovery and store the result."""

    async def main() -> None:
        async with _context(account, discover=True) as c:
            d = c.registry.last_discovery
            _emit(
                {
                    "build": d.build if d else None,
                    "scripts_fetched": d.scripts_fetched if d else 0,
                    "missing": d.missing if d else None,
                    "operations": {
                        n: {"query_id": s.query_id, "features": len(s.features), "source": s.source}
                        for n, s in c.registry.snapshot().items()
                    },
                }
            )

    _run(main)


@x.command("search")
@click.argument("query")
@click.option("--tab", type=click.Choice(["latest", "top", "people"]), default="latest")
@click.option("--pages", default=1, show_default=True, type=click.IntRange(1, 5))
@click.option("--bucket", "bucket_name", default="POST/main", show_default=True)
@account_opt
@discover_opt
def x_search(query: str, tab: str, pages: int, bucket_name: str, account: str | None, discover: bool) -> None:
    """Search tweets (latest/top) or users (people)."""
    bucket = _bucket(bucket_name)

    async def main() -> None:
        async with _context(account, discover) as c:
            items: list[Any] = []
            cursor = None
            for _ in range(pages):
                o = await c.client.call(ops.search(query, tab, cursor=cursor), bucket)  # type: ignore[arg-type]
                _meta(o)
                if not o.ok:
                    break
                items.extend(i.model_dump(mode="json") for i in o.parsed.items)
                cursor = o.parsed.cursor_bottom
                click.echo(f"# page stats: {o.parsed.stats.model_dump()}", err=True)
                if not cursor or not o.parsed.items:
                    break
            _emit(items)

    _run(main)


@x.command("user")
@click.argument("username")
@click.option("--bucket", "bucket_name", default="GET/main", show_default=True)
@account_opt
@discover_opt
def x_user(username: str, bucket_name: str, account: str | None, discover: bool) -> None:
    """Fetch a user profile."""
    bucket = _bucket(bucket_name)

    async def main() -> None:
        async with _context(account, discover) as c:
            o = await c.client.call(ops.user_by_screen_name(username), bucket)
            _meta(o)
            _emit(o.parsed.model_dump(mode="json") if o.parsed else None)

    _run(main)


@x.command("user-tweets")
@click.argument("username")
@click.option("--pages", default=1, show_default=True, type=click.IntRange(1, 5))
@click.option("--bucket", "bucket_name", default="GET/main", show_default=True)
@account_opt
@discover_opt
def x_user_tweets(username: str, pages: int, bucket_name: str, account: str | None, discover: bool) -> None:
    """Fetch latest tweets of a user (profile lookup + UserTweets)."""
    bucket = _bucket(bucket_name)

    async def main() -> None:
        async with _context(account, discover) as c:
            prof = await c.client.call(ops.user_by_screen_name(username), bucket)
            _meta(prof)
            if not prof.parsed:
                raise click.ClickException(f"user {username} not found")
            items: list[Any] = []
            cursor = None
            for _ in range(pages):
                o = await c.client.call(ops.user_tweets(prof.parsed.id, cursor=cursor), bucket)
                _meta(o)
                if not o.ok:
                    break
                items.extend(i.model_dump(mode="json") for i in o.parsed.items)
                cursor = o.parsed.cursor_bottom
                if not cursor or not o.parsed.items:
                    break
            _emit(items)

    _run(main)


@x.command("burst")
@click.argument("queries", nargs=-1, required=True)
@click.option("--repeat", default=1, show_default=True, type=click.IntRange(1, 10))
@click.option("--consumer", default="cli", show_default=True)
def x_burst(queries: tuple[str, ...], repeat: int, consumer: str) -> None:
    """Run searches concurrently through the gateway and show how they spread over accounts and buckets."""
    from collections import Counter

    from xscout.gateway.gateway import Unavailable
    from xscout.gateway.runtime import Runtime

    async def main() -> None:
        rt = Runtime(get_settings())
        await rt.start(background=False)
        try:
            jobs = [q for q in queries for _ in range(repeat)]

            async def one(q: str):
                try:
                    r = await rt.gateway.run(lambda cur, q=q: ops.search(q, cursor=cur), consumer=consumer)
                    return q, r.meta, len(r.pages[0].items) if r.pages else 0, None
                except Unavailable as e:
                    return q, None, 0, f"unavailable: {e.reason} (retry {e.retry_after_sec}s)"

            results = await asyncio.gather(*(one(q) for q in jobs))
            for q, meta, n, err in results:
                if err:
                    click.echo(f"# {q!r}: {err}", err=True)
                else:
                    click.echo(
                        f"# {q!r}: items={n} cached={meta.cached} shared={meta.shared} attempts={meta.attempts}",
                        err=True,
                    )
            spread = Counter((r.account_id, r.bucket, r.outcome) for r in rt._pending_requests)
            names = {a.id: a.username for a in rt.pool.accounts.values()}
            _emit(
                {
                    "requests_sent": len(rt._pending_requests),
                    "spread": [
                        {"account": names.get(a), "bucket": b, "outcome": o, "count": c}
                        for (a, b, o), c in sorted(spread.items(), key=lambda kv: str(kv[0]))
                    ],
                    "cache": rt.cache.stats(),
                    "gateway": rt.gateway.stats,
                }
            )
        finally:
            await rt.stop()

    _run(main)


@click.command("pool")
def pool_cmd() -> None:
    """Show accounts with their stored quota state per operation and bucket."""
    from xscout.gateway.runtime import Runtime

    async def main() -> None:
        rt = Runtime(get_settings())
        await rt.start(background=False)
        try:
            _emit(rt.pool.snapshot())
        finally:
            await rt.stop()

    _run(main)


if __name__ == "__main__":
    x(sys.argv[1:])
