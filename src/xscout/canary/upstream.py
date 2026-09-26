"""Upstream watch: new commits in reference projects that touch the fragile parts (spec §6, plan 6.3).

Early warning only; nothing is applied automatically. Paths were checked against each repository's
layout on 2026-09-26. Uses the public GitHub REST API (60 requests/hour unauthenticated; set
XSCOUT_GITHUB_TOKEN for more).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from curl_cffi.requests import AsyncSession

STATE_KEY = "upstream:last_checked"

WATCHED: dict[str, list[str]] = {
    "vladkens/twscrape": ["twscrape/xclid.py", "twscrape/api.py", "twscrape/models.py", "twscrape/queue_client.py"],
    "iSarabjitDhiman/XClientTransaction": ["x_client_transaction"],
    "zedeus/nitter": ["src/tid.nim", "src/consts.nim", "src/parser.nim", "src/apiutils.nim"],
    "Lqm1/x-client-transaction-id": ["transaction.ts", "utils.ts"],
}

FetchJson = Callable[[str, dict[str, str]], Awaitable[tuple[int, Any]]]


@dataclass(frozen=True)
class Change:
    repo: str
    sha: str
    date: str
    message: str
    paths: tuple[str, ...]
    url: str


async def github_json(url: str, params: dict[str, str]) -> tuple[int, Any]:
    headers = {
        "accept": "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "xscout-upstream-watch",  # GitHub answers 403 to requests without a User-Agent
    }
    if token := os.environ.get("XSCOUT_GITHUB_TOKEN"):
        headers["authorization"] = f"Bearer {token}"
    async with AsyncSession(timeout=20) as s:
        rep = await s.get(url, params=params, headers=headers)
    try:
        return rep.status_code, rep.json()
    except ValueError:
        return rep.status_code, {"message": rep.text[:200]}


async def check(
    since: datetime, fetch: FetchJson = github_json, watched: dict[str, list[str]] = WATCHED
) -> tuple[list[Change], list[str]]:
    """Commits since `since` per watched path, merged per commit. Returns (changes, errors)."""
    found: dict[tuple[str, str], dict] = {}
    errors: list[str] = []
    for repo, paths in watched.items():
        for path in paths:
            status, body = await fetch(
                f"https://api.github.com/repos/{repo}/commits",
                {"path": path, "since": since.isoformat(), "per_page": "30"},
            )
            if status != 200 or not isinstance(body, list):
                message = body.get("message") if isinstance(body, dict) else ""
                errors.append(f"{repo}:{path}: HTTP {status} {message}".strip())
                continue
            for c in body:
                key = (repo, c["sha"])
                entry = found.setdefault(
                    key,
                    {
                        "date": c["commit"]["committer"]["date"],
                        "message": (c["commit"]["message"] or "").splitlines()[0][:200],
                        "url": c.get("html_url", ""),
                        "paths": set(),
                    },
                )
                entry["paths"].add(path)
    changes = [
        Change(repo, sha, e["date"], e["message"], tuple(sorted(e["paths"])), e["url"])
        for (repo, sha), e in found.items()
    ]
    changes.sort(key=lambda c: c.date, reverse=True)
    return changes, errors


async def check_since_last(kv: Any, default_days: int = 30, fetch: FetchJson = github_json) -> dict[str, Any]:
    """Run `check` from the stored cursor and advance it when every request succeeded."""
    now = datetime.now(UTC)
    stored = await kv.get(STATE_KEY)
    since = datetime.fromisoformat(stored) if stored else now - timedelta(days=default_days)
    changes, errors = await check(since, fetch)
    if not errors:
        await kv.set(STATE_KEY, now.isoformat())
    return {
        "since": since.isoformat(),
        "checked_at": now.isoformat(),
        "changes": [c.__dict__ | {"paths": list(c.paths)} for c in changes],
        "errors": errors,
    }
