"""Loopback HTTP API for the bots (spec §7, plan 4.1-4.2)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from xscout import __version__
from xscout.api.service import NotFound, XService
from xscout.gateway.gateway import BadRequest, Unavailable
from xscout.xweb.ops import MAX_QUERY_CHARS, QueryTooLong

CONSUMER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
USERNAME_RE = r"^@?[A-Za-z0-9_]{1,15}$"


class StatusProvider(Protocol):
    def health_report(self) -> dict: ...
    def accounts_report(self) -> list[dict]: ...
    def budget_report(self) -> dict: ...


@dataclass
class Backend:
    service: XService
    status: StatusProvider


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after_sec: int | None = None):
        self.status, self.code, self.message, self.retry_after_sec = status, code, message, retry_after_sec


def _error(status: int, code: str, message: str, retry_after_sec: int | None = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    headers = {}
    if retry_after_sec is not None:
        body["error"]["retry_after_sec"] = retry_after_sec
        headers["Retry-After"] = str(retry_after_sec)
    return JSONResponse(body, status_code=status, headers=headers)


def consumer(x_consumer: Annotated[str | None, Header()] = None) -> str:
    value = (x_consumer or "").strip().lower()
    if not CONSUMER_RE.fullmatch(value):
        raise ApiError(400, "missing_consumer", "header X-Consumer is required (e.g. cdexio, zetryn, stocks)")
    return value


def backend(request: Request) -> Backend:
    return request.app.state.backend


Svc = Annotated[Backend, Depends(backend)]
Consumer = Annotated[str, Depends(consumer)]
Limit = Annotated[int, Query(ge=1, le=200)]
MaxAge = Annotated[float | None, Query(ge=0, description="accept cached data up to this age")]
Cursor = Annotated[str | None, Query(max_length=2048)]


def create_app(open_backend: Callable[[], AbstractAsyncContextManager[Backend]]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_backend() as backend:
            app.state.backend = backend
            yield

    app = FastAPI(
        title="xscout",
        version=__version__,
        description="Self-hosted X search service shared by the trading bots. Loopback only.",
        lifespan=lifespan,
    )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError) -> JSONResponse:
        return _error(e.status, e.code, e.message, e.retry_after_sec)

    @app.exception_handler(Unavailable)
    async def _unavailable(_: Request, e: Unavailable) -> JSONResponse:
        return _error(503, "unavailable", e.reason, e.retry_after_sec)

    @app.exception_handler(BadRequest)
    async def _bad(_: Request, e: BadRequest) -> JSONResponse:
        return _error(400, "bad_request", str(e))

    @app.exception_handler(QueryTooLong)
    async def _too_long(_: Request, e: QueryTooLong) -> JSONResponse:
        return _error(400, "query_too_long", str(e))

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, e: NotFound) -> JSONResponse:
        return _error(404, "not_found", str(e))

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError) -> JSONResponse:
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x not in ("query", "path"))
        return _error(400, "invalid_parameter", f"{where}: {first.get('msg', 'invalid')}")

    @app.get("/health", tags=["status"])
    async def health(b: Svc) -> dict:
        return b.status.health_report()

    @app.get("/v1/accounts", tags=["status"])
    async def accounts(b: Svc) -> list[dict]:
        return b.status.accounts_report()

    @app.get("/v1/budget", tags=["status"])
    async def budget(b: Svc) -> dict:
        return b.status.budget_report()

    @app.get("/v1/search/tweets", tags=["search"])
    async def search_tweets(
        b: Svc,
        who: Consumer,
        q: Annotated[str, Query(min_length=1, max_length=MAX_QUERY_CHARS, description="X search operators allowed")],
        tab: Literal["latest", "top"] = "latest",
        limit: Limit = 20,
        cursor: Cursor = None,
        max_age_sec: MaxAge = None,
    ) -> dict:
        env = await b.service.search_tweets(q, tab, limit, who, cursor=cursor, max_age_sec=max_age_sec)
        return env.to_json()

    @app.get("/v1/search/users", tags=["search"])
    async def search_users(
        b: Svc,
        who: Consumer,
        q: Annotated[str, Query(min_length=1, max_length=MAX_QUERY_CHARS)],
        limit: Limit = 20,
        cursor: Cursor = None,
        max_age_sec: MaxAge = None,
    ) -> dict:
        env = await b.service.search_users(q, limit, who, cursor=cursor, max_age_sec=max_age_sec)
        return env.to_json()

    @app.get("/v1/users/{username}", tags=["users"])
    async def user(b: Svc, who: Consumer, username: str, max_age_sec: MaxAge = None) -> dict:
        _check_username(username)
        return (await b.service.user(username.lstrip("@"), who, max_age_sec=max_age_sec)).to_json()

    @app.get("/v1/users/{username}/tweets", tags=["users"])
    async def user_tweets(
        b: Svc, who: Consumer, username: str, limit: Limit = 20, cursor: Cursor = None, max_age_sec: MaxAge = None
    ) -> dict:
        _check_username(username)
        env = await b.service.user_tweets(username.lstrip("@"), limit, who, cursor=cursor, max_age_sec=max_age_sec)
        return env.to_json()

    return app


def _check_username(username: str) -> None:
    if not re.fullmatch(USERNAME_RE, username):
        raise ApiError(400, "invalid_parameter", f"username: {username!r} is not a valid X username")
