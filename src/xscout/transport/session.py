"""Per-account curl_cffi session (spec §4 transport, plan 2.1).

The transport sends what it is given and reports status, headers, body and timing. It never
interprets X errors; that is the gateway's job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

from xscout.log import redactor
from xscout.store.accounts import AccountCredentials


class TransportError(Exception):
    """Network-level failure (DNS, connect, TLS, timeout). The account is not at fault."""


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    url: str
    params: dict[str, str] | None = None
    body: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    csrf: bool = True  # add x-csrf-token from the session's current ct0 cookie


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]
    text: str
    elapsed_ms: int
    url: str

    def json(self) -> Any:
        import json

        return json.loads(self.text)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


class AccountTransport:
    """One long-lived session per account: fixed impersonation profile, optional proxy, own cookie jar."""

    def __init__(self, creds: AccountCredentials, timeout_sec: float = 30.0):
        self.account_id = creds.id
        self.username = creds.username
        self.impersonate = creds.impersonate
        redactor.register(creds.auth_token, creds.ct0, creds.proxy)
        self._initial_ct0 = creds.ct0
        self._session = AsyncSession(
            impersonate=creds.impersonate,  # type: ignore[arg-type]
            proxy=creds.proxy,
            allow_redirects=True,
            timeout=timeout_sec,
        )
        self._session.cookies.set("auth_token", creds.auth_token, domain=".x.com")
        self._session.cookies.set("ct0", creds.ct0, domain=".x.com")

    @property
    def ct0(self) -> str:
        """Current csrf cookie; X may rotate it through Set-Cookie."""
        value = self._session.cookies.get("ct0", domain=".x.com") or self._session.cookies.get("ct0")
        return value or self._initial_ct0

    @property
    def ct0_rotated(self) -> bool:
        return self.ct0 != self._initial_ct0

    async def send(self, req: PreparedRequest) -> RawResponse:
        headers = dict(req.headers)
        if req.csrf:
            headers["x-csrf-token"] = self.ct0
        t0 = time.monotonic()
        try:
            rep = await self._session.request(
                req.method,  # type: ignore[arg-type]
                req.url,
                params=req.params,
                data=req.body,
                headers=headers,
            )
        except RequestsError as e:
            raise TransportError(redactor.redact(str(e))) from e
        elapsed = int((time.monotonic() - t0) * 1000)
        new_ct0 = self.ct0
        if new_ct0 != self._initial_ct0:
            redactor.register(new_ct0)
        return RawResponse(
            status=rep.status_code,
            headers={k.lower(): v for k, v in rep.headers.items()},
            text=rep.text,
            elapsed_ms=elapsed,
            url=str(rep.url),
        )

    async def get_text(self, url: str) -> RawResponse:
        """Plain page or bundle fetch with the account's cookies (no API headers)."""
        return await self.send(PreparedRequest("GET", url, csrf=False))

    async def close(self) -> None:
        await self._session.close()
