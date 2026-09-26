"""Shared fixtures. DB tests run only against a database named xscout_test."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xscout.store.db import make_engine, make_sessionmaker

PROJECT_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "xscout_test"
TABLES_SQL = text("select tablename from pg_tables where schemaname='public' and tablename <> 'alembic_version'")


def _test_db_url() -> str | None:
    url = os.environ.get("XSCOUT_TEST_DATABASE_URL")
    if not url:
        env_file = PROJECT_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("XSCOUT_TEST_DATABASE_URL="):
                    url = line.split("=", 1)[1].strip()
    return url or None


def guard_test_url(url: str) -> str:
    """Refuse any database that is not xscout_test (protects the real data)."""
    name = make_url(url).database
    if name != TEST_DB_NAME:
        raise RuntimeError(f"refusing to run DB tests on database {name!r}; expected {TEST_DB_NAME!r}")
    return url


def alembic(url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"url={url}", *args],
        cwd=PROJECT_DIR,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def db_url() -> str:
    url = _test_db_url()
    if not url:
        pytest.skip("XSCOUT_TEST_DATABASE_URL not set")
    if "<password>" in url:
        pytest.skip("XSCOUT_TEST_DATABASE_URL still has the <password> placeholder")
    return guard_test_url(url)


@pytest.fixture(scope="session")
def migrated(db_url: str) -> str:
    alembic(db_url, "downgrade", "base")
    alembic(db_url, "upgrade", "head")
    return db_url


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(migrated)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        rows = await conn.execute(TABLES_SQL)
        tables = rows.scalars().all()
        if tables:
            await conn.execute(text(f"truncate {', '.join(tables)} restart identity cascade"))
    yield make_sessionmaker(engine)
