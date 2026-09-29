"""Account repository: encrypted cookie storage and status changes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xscout.crypto import SecretBox
from xscout.store.models import Account, AccountStatus

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
COOKIE_VALUE_RE = re.compile(r"^[A-Za-z0-9]{20,512}$")


class AccountError(ValueError):
    pass


def normalize_username(raw: str) -> str:
    name = raw.strip().lstrip("@")
    if not USERNAME_RE.fullmatch(name):
        raise AccountError(f"invalid X username: {raw!r}")
    return name.lower()


def validate_cookie(name: str, value: str) -> str:
    value = value.strip().strip('"').strip("'")
    if not COOKIE_VALUE_RE.fullmatch(value):
        raise AccountError(f"{name} does not look like an X cookie value")
    return value


def parse_cookie_string(raw: str) -> dict[str, str]:
    """Parse `auth_token=...; ct0=...` as copied from a browser."""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def pick_impersonate(username: str, profiles: list[str]) -> str:
    """Fixed browser profile per account, stable across restarts."""
    if not profiles:
        raise AccountError("pool.impersonate_profiles is empty")
    digest = int(hashlib.sha256(username.encode()).hexdigest()[:8], 16)
    return profiles[digest % len(profiles)]


@dataclass(frozen=True)
class AccountView:
    """Account without secrets, safe to print or return from the API."""

    id: int
    username: str
    status: str
    status_reason: str | None
    impersonate: str
    has_proxy: bool
    allow_overflow: bool
    cooling_until: datetime | None
    last_used_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    created_at: datetime

    @staticmethod
    def of(a: Account) -> AccountView:
        return AccountView(
            id=a.id,
            username=a.username,
            status=a.status,
            status_reason=a.status_reason,
            impersonate=a.impersonate,
            has_proxy=a.proxy_enc is not None,
            allow_overflow=a.allow_overflow,
            cooling_until=a.cooling_until,
            last_used_at=a.last_used_at,
            last_error=a.last_error,
            last_error_at=a.last_error_at,
            created_at=a.created_at,
        )


@dataclass(frozen=True)
class AccountCredentials:
    id: int
    username: str
    auth_token: str
    ct0: str
    proxy: str | None
    impersonate: str
    allow_overflow: bool


class AccountRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], box: SecretBox):
        self._sessions = sessions
        self._box = box

    async def add(
        self,
        username: str,
        auth_token: str,
        ct0: str,
        impersonate_profiles: list[str],
        proxy: str | None = None,
        replace: bool = False,
    ) -> AccountView:
        name = normalize_username(username)
        token = validate_cookie("auth_token", auth_token)
        csrf = validate_cookie("ct0", ct0)
        async with self._sessions.begin() as s:
            existing = await s.scalar(select(Account).where(Account.username == name))
            if existing and not replace:
                raise AccountError(f"account {name} already exists (use replace to update its cookies)")
            acc = existing or Account(username=name, impersonate=pick_impersonate(name, impersonate_profiles))
            acc.auth_token_enc = self._box.encrypt(token)
            acc.ct0_enc = self._box.encrypt(csrf)
            acc.proxy_enc = self._box.encrypt(proxy) if proxy else None
            acc.status = AccountStatus.ACTIVE
            acc.status_reason = "cookies added" if existing is None else "cookies replaced"
            acc.status_changed_at = datetime.now(UTC)
            acc.last_error = None
            acc.cooling_until = None
            s.add(acc)
            await s.flush()
            await s.refresh(acc)
            return AccountView.of(acc)

    async def list(self) -> list[AccountView]:
        async with self._sessions() as s:
            rows = (await s.scalars(select(Account).order_by(Account.username))).all()
            return [AccountView.of(a) for a in rows]

    async def get(self, username: str) -> AccountView | None:
        name = normalize_username(username)
        async with self._sessions() as s:
            acc = await s.scalar(select(Account).where(Account.username == name))
            return AccountView.of(acc) if acc else None

    async def credentials(self, username: str) -> AccountCredentials:
        name = normalize_username(username)
        async with self._sessions() as s:
            acc = await s.scalar(select(Account).where(Account.username == name))
            if acc is None:
                raise AccountError(f"account {name} not found")
            return AccountCredentials(
                id=acc.id,
                username=acc.username,
                auth_token=self._box.decrypt(acc.auth_token_enc),
                ct0=self._box.decrypt(acc.ct0_enc),
                proxy=self._box.decrypt(acc.proxy_enc) if acc.proxy_enc else None,
                impersonate=acc.impersonate,
                allow_overflow=acc.allow_overflow,
            )

    async def set_status(self, username: str, status: AccountStatus, reason: str | None = None) -> AccountView:
        name = normalize_username(username)
        async with self._sessions.begin() as s:
            acc = await s.scalar(select(Account).where(Account.username == name))
            if acc is None:
                raise AccountError(f"account {name} not found")
            acc.status = status
            acc.status_reason = reason
            acc.status_changed_at = datetime.now(UTC)
            await s.flush()
            await s.refresh(acc)
            return AccountView.of(acc)

    async def set_overflow(self, username: str, allowed: bool) -> AccountView:
        name = normalize_username(username)
        async with self._sessions.begin() as s:
            acc = await s.scalar(select(Account).where(Account.username == name))
            if acc is None:
                raise AccountError(f"account {name} not found")
            acc.allow_overflow = allowed
            await s.flush()
            await s.refresh(acc)
            return AccountView.of(acc)

    async def active_credentials(self) -> list[AccountCredentials]:
        async with self._sessions() as s:
            rows = (await s.scalars(select(Account).where(Account.status == AccountStatus.ACTIVE))).all()
            return [
                AccountCredentials(
                    id=a.id,
                    username=a.username,
                    auth_token=self._box.decrypt(a.auth_token_enc),
                    ct0=self._box.decrypt(a.ct0_enc),
                    proxy=self._box.decrypt(a.proxy_enc) if a.proxy_enc else None,
                    impersonate=a.impersonate,
                    allow_overflow=a.allow_overflow,
                )
                for a in rows
            ]

    async def set_status_by_id(self, account_id: int, status: AccountStatus, reason: str | None) -> None:
        async with self._sessions.begin() as s:
            acc = await s.get(Account, account_id)
            if acc is not None:
                acc.status = status
                acc.status_reason = reason
                acc.status_changed_at = datetime.now(UTC)

    async def update_ct0(self, account_id: int, ct0: str) -> None:
        """X rotated the csrf cookie; keep the stored copy current."""
        value = validate_cookie("ct0", ct0)
        async with self._sessions.begin() as s:
            acc = await s.get(Account, account_id)
            if acc is not None:
                acc.ct0_enc = self._box.encrypt(value)

    async def record_use(
        self,
        account_id: int,
        used_at: datetime,
        error: str | None = None,
        cooling_until: datetime | None = None,
    ) -> None:
        async with self._sessions.begin() as s:
            acc = await s.get(Account, account_id)
            if acc is None:
                return
            acc.last_used_at = used_at
            if error:
                acc.last_error = error[:1000]
                acc.last_error_at = used_at
            if cooling_until is not None:
                acc.cooling_until = cooling_until

    # MARK: moving accounts between machines (ciphertext only)

    KEY_CHECK = "xscout-key-check"

    async def export_rows(self) -> dict:
        """Accounts as stored (encrypted) plus a token that proves which key encrypted them."""
        async with self._sessions() as s:
            rows = (await s.scalars(select(Account).order_by(Account.username))).all()
            return {
                "format": "xscout-accounts/1",
                "key_check": self._box.encrypt(self.KEY_CHECK),
                "accounts": [
                    {
                        "username": a.username,
                        "auth_token_enc": a.auth_token_enc,
                        "ct0_enc": a.ct0_enc,
                        "proxy_enc": a.proxy_enc,
                        "impersonate": a.impersonate,
                        "allow_overflow": a.allow_overflow,
                        "status": a.status,
                        "status_reason": a.status_reason,
                    }
                    for a in rows
                ],
            }

    async def import_rows(
        self, data: dict, replace: bool = False, status: AccountStatus | None = None
    ) -> dict[str, list[str]]:
        """Insert exported accounts. Refuses when this machine's key cannot decrypt them."""
        if data.get("format") != "xscout-accounts/1":
            raise AccountError("not an xscout accounts export")
        if self._box.decrypt(data["key_check"]) != self.KEY_CHECK:
            raise AccountError("export was made with a different XSCOUT_SECRET_KEY")
        result: dict[str, list[str]] = {"added": [], "replaced": [], "skipped": []}
        async with self._sessions.begin() as s:
            for row in data["accounts"]:
                name = normalize_username(row["username"])
                for field in ("auth_token_enc", "ct0_enc", "proxy_enc"):
                    if row.get(field):
                        self._box.decrypt(row[field])  # fail loudly on a corrupted value, print nothing
                acc = await s.scalar(select(Account).where(Account.username == name))
                if acc is not None and not replace:
                    result["skipped"].append(name)
                    continue
                result["replaced" if acc is not None else "added"].append(name)
                acc = acc or Account(username=name)
                acc.auth_token_enc = row["auth_token_enc"]
                acc.ct0_enc = row["ct0_enc"]
                acc.proxy_enc = row.get("proxy_enc")
                acc.impersonate = row["impersonate"]
                acc.allow_overflow = bool(row.get("allow_overflow", True))
                acc.status = status or row.get("status") or AccountStatus.ACTIVE
                acc.status_reason = "imported" if status is None else f"imported as {status}"
                acc.status_changed_at = datetime.now(UTC)
                acc.cooling_until = None
                s.add(acc)
        return result

    async def remove(self, username: str) -> bool:
        name = normalize_username(username)
        async with self._sessions.begin() as s:
            res = await s.execute(delete(Account).where(Account.username == name))
            return (res.rowcount or 0) > 0
