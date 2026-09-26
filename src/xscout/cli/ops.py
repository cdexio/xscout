"""Operations commands: canary, upstream watch, retention, response samples (plan 6.1-6.4)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import click

from xscout.config import PROJECT_DIR, get_settings
from xscout.log import redactor

FIXTURES_DIR = PROJECT_DIR / "tests" / "fixtures" / "x"
FIXTURE_NAME_RE = re.compile(r"^[a-z0-9_]{3,64}$")


def _emit(obj) -> None:
    click.echo(json.dumps(obj, default=str, ensure_ascii=False, indent=2))


async def _with_runtime(fn):
    from xscout.gateway.runtime import Runtime

    rt = Runtime(get_settings())
    await rt.start(background=False)
    try:
        return await fn(rt)
    finally:
        await rt.stop()


@click.command("canary")
def canary_cmd() -> None:
    """Run the canary probes once (spends a few P2 requests) and print component states."""
    _emit(asyncio.run(_with_runtime(lambda rt: rt.run_canary())))


@click.command("upstream")
@click.option("--days", default=30, show_default=True, help="Look back this far on the first run.")
def upstream_cmd(days: int) -> None:
    """List new upstream commits touching TID, queryId/features or parser code since the last check."""
    from xscout.canary.upstream import check_since_last
    from xscout.store.db import make_engine, make_sessionmaker
    from xscout.store.maintenance import KvRepository

    async def main():
        engine = make_engine(get_settings().database_url)
        try:
            return await check_since_last(KvRepository(make_sessionmaker(engine)), default_days=days)
        finally:
            await engine.dispose()

    report = asyncio.run(main())
    _emit(report)
    if report["errors"]:
        raise click.ClickException("some repositories could not be checked; the cursor was not advanced")


@click.command("retention")
def retention_cmd() -> None:
    """Delete rows past their retention now (tweets, feed, samples, request log)."""
    _emit(asyncio.run(_with_runtime(lambda rt: rt.run_retention())))


@click.group("samples")
def samples() -> None:
    """Raw X responses stored on parse problems."""


def _reader():
    from xscout.store.db import make_engine, make_sessionmaker
    from xscout.store.maintenance import SampleReader

    engine = make_engine(get_settings().database_url)
    return engine, SampleReader(make_sessionmaker(engine))


@samples.command("list")
@click.option("--limit", default=20, show_default=True)
def samples_list(limit: int) -> None:
    async def main():
        engine, reader = _reader()
        try:
            return await reader.recent(limit)
        finally:
            await engine.dispose()

    _emit(asyncio.run(main()))


@samples.command("promote")
@click.argument("sample_id", type=int)
@click.argument("name")
def samples_promote(sample_id: int, name: str) -> None:
    """Save a sample as tests/fixtures/x/<name>.json (redacted, pretty) to use as a regression fixture."""
    if not FIXTURE_NAME_RE.fullmatch(name):
        raise click.BadParameter("name: lowercase letters, digits and _ (3-64)")
    target = FIXTURES_DIR / f"{name}.json"
    if target.exists():
        raise click.ClickException(f"{target} already exists")

    async def main():
        engine, reader = _reader()
        try:
            return await reader.body(sample_id)
        finally:
            await engine.dispose()

    body = asyncio.run(main())
    if body is None:
        raise click.ClickException(f"sample {sample_id} not found or empty")
    try:
        text = json.dumps(json.loads(body), indent=2, ensure_ascii=False) + "\n"
    except ValueError as e:
        raise click.ClickException("sample body is not JSON; nothing written") from e
    Path(target).write_text(redactor.redact(text))
    click.echo(f"wrote {target}")
