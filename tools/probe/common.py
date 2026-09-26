"""Shared helpers for phase 0 probes: account, session, GraphQL call, logging."""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

PROBE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PROBE_DIR.parents[1]
OUT_DIR = PROBE_DIR / "out"
FIXTURES_DIR = PROJECT_DIR / "tests" / "fixtures" / "x"

# Public web-client bearer tokens (same values in twscrape, twikit and Nitter).
BEARER_MAIN = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
BEARER_ALT = (
    "Bearer AAAAAAAAAAAAAAAAAAAAAFXzAwAAAAAAMHCxpeSDG1gLNLghVe8d74hl6k4"
    "%3DRUMF4xAQLsbeBhTSRrCiQpJtxoGWeyHrDb5te2jpGskWDFW82F"
)
GQL_URL = "https://x.com/i/api/graphql"
IMPERSONATE = "chrome"
MIN_REMAINING = 10
GAP_SEC = (3.0, 5.0)


@dataclass
class Account:
    username: str
    auth_token: str
    ct0: str

    @staticmethod
    def load() -> "Account":
        env: dict[str, str] = {}
        for line in (PROJECT_DIR / ".env.probe").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        return Account(
            username=env["XSCOUT_PROBE_USERNAME"].lstrip("@"),
            auth_token=env["XSCOUT_PROBE_AUTH_TOKEN"],
            ct0=env["XSCOUT_PROBE_CT0"],
        )

    def redact(self, text: str) -> str:
        for secret, label in ((self.auth_token, "<AUTH_TOKEN>"), (self.ct0, "<CT0>")):
            text = text.replace(secret, label)
        return text


def make_session(acc: Account) -> AsyncSession:
    session = AsyncSession(impersonate=IMPERSONATE, allow_redirects=True, timeout=30)
    session.cookies.set("auth_token", acc.auth_token, domain=".x.com")
    session.cookies.set("ct0", acc.ct0, domain=".x.com")
    return session


def api_headers(acc: Account, bearer: str, method: str) -> dict[str, str]:
    headers = {
        "authorization": bearer,
        "content-type": "application/json",
        "x-csrf-token": acc.ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "referer": "https://x.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if method == "POST":
        headers["origin"] = "https://x.com"
    return headers


def compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


@dataclass
class CallResult:
    label: str
    op: str
    method: str
    status: int
    limit: int | None
    remaining: int | None
    reset: int | None
    error_codes: list[int]
    error_messages: list[str]
    content_type: str
    elapsed_ms: int
    body: Any = field(repr=False, default=None)
    text: str = field(repr=False, default="")

    def summary(self) -> dict:
        return {
            "label": self.label,
            "op": self.op,
            "method": self.method,
            "status": self.status,
            "rate": f"{self.remaining}/{self.limit}" if self.limit is not None else None,
            "reset_in_sec": (self.reset - int(time.time())) if self.reset else None,
            "error_codes": self.error_codes,
            "error_messages": [m[:160] for m in self.error_messages],
            "content_type": self.content_type,
            "elapsed_ms": self.elapsed_ms,
        }


class Prober:
    """Paced GraphQL caller that refuses to push any operation below MIN_REMAINING."""

    def __init__(self, acc: Account, session: AsyncSession):
        self.acc = acc
        self.session = session
        self.remaining: dict[str, int] = {}
        self.results: list[CallResult] = []
        self._last_call = 0.0

    async def _pace(self) -> None:
        wait = self._last_call + random.uniform(*GAP_SEC) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def gql(
        self,
        label: str,
        op_id: str,
        op_name: str,
        variables: dict,
        features: dict,
        field_toggles: dict | None = None,
        method: str = "GET",
        tid_gen: Any = None,
        bearer: str = BEARER_MAIN,
    ) -> CallResult | None:
        if self.remaining.get(op_name, 999) <= MIN_REMAINING:
            print(f"  skip {label}: {op_name} remaining={self.remaining[op_name]} <= {MIN_REMAINING}")
            return None
        await self._pace()
        url = f"{GQL_URL}/{op_id}/{op_name}"
        headers = api_headers(self.acc, bearer, method)
        if tid_gen is not None:
            headers["x-client-transaction-id"] = tid_gen.calc(method, urlparse(url).path)
        t0 = time.monotonic()
        if method == "GET":
            params = {"variables": compact(variables), "features": compact(features)}
            if field_toggles:
                params["fieldToggles"] = compact(field_toggles)
            rep = await self.session.get(url, params=params, headers=headers)
        else:
            payload: dict[str, Any] = {"variables": variables, "features": features, "queryId": op_id}
            if field_toggles:
                payload["fieldToggles"] = field_toggles
            rep = await self.session.post(url, data=compact(payload), headers=headers)
        elapsed = int((time.monotonic() - t0) * 1000)

        def hdr_int(name: str) -> int | None:
            v = rep.headers.get(name)
            return int(v) if v is not None and str(v).lstrip("-").isdigit() else None

        body: Any = None
        codes: list[int] = []
        messages: list[str] = []
        try:
            body = rep.json()
            for err in (body.get("errors") or []) if isinstance(body, dict) else []:
                if "code" in err:
                    codes.append(int(err["code"]))
                messages.append(str(err.get("message", "")))
        except Exception:
            body = None
        res = CallResult(
            label=label,
            op=op_name,
            method=method,
            status=rep.status_code,
            limit=hdr_int("x-rate-limit-limit"),
            remaining=hdr_int("x-rate-limit-remaining"),
            reset=hdr_int("x-rate-limit-reset"),
            error_codes=codes,
            error_messages=messages,
            content_type=str(rep.headers.get("content-type", "")),
            elapsed_ms=elapsed,
            body=body,
            text=self.acc.redact(rep.text[:2000]) if body is None else "",
        )
        if res.remaining is not None:
            self.remaining[op_name] = res.remaining
        self.results.append(res)
        print(f"  {label}: {compact(res.summary())}")
        return res


def save_json(path: Path, obj: Any, acc: Account | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if acc is not None:
        text = acc.redact(text)
    path.write_text(text + "\n")
