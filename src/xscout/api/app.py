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
from pydantic import BaseModel, Field

from xscout import __version__
from xscout.api.service import NotFound, XService
from xscout.api.watch import OverCapacity, WatchService
from xscout.gateway.gateway import BadRequest, Unavailable
from xscout.scheduler.units import WatchValueError
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
    watch: WatchService | None = None


class WatchCreate(BaseModel):
    kind: Literal["user", "query"]
    value: str = Field(min_length=1, max_length=600)
    interval_sec: int
    tags: list[str] = Field(default_factory=list, max_length=20)


class WatchPatch(BaseModel):
    interval_sec: int | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    enabled: bool | None = None


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

    @app.exception_handler(WatchValueError)
    async def _watch_value(_: Request, e: WatchValueError) -> JSONResponse:
        return _error(400, "invalid_parameter", str(e))

    @app.exception_handler(OverCapacity)
    async def _over_capacity(_: Request, e: OverCapacity) -> JSONResponse:
        body = {
            "error": {
                "code": "over_capacity",
                "message": str(e),
                "needed_per_15m": round(e.needed, 1),
                "p1_capacity_per_15m": round(e.available, 1),
            }
        }
        return JSONResponse(body, status_code=409)

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

    # MARK: watchlist and feed

    def watch_of(b: Backend) -> WatchService:
        if b.watch is None:
            raise ApiError(503, "unavailable", "watchlist is not enabled", 60)
        return b.watch

    @app.post("/v1/watchlist", tags=["watchlist"], status_code=201)
    async def watch_create(b: Svc, who: Consumer, body: WatchCreate) -> JSONResponse:
        item, created = await watch_of(b).create(body.kind, body.value, body.interval_sec, body.tags, who)
        return JSONResponse({"data": item, "created": created}, status_code=201 if created else 200)

    @app.get("/v1/watchlist", tags=["watchlist"])
    async def watch_list(
        b: Svc,
        who: Consumer,
        mine: bool = False,
        tag: Annotated[str | None, Query(max_length=32)] = None,
    ) -> dict:
        return {"data": await watch_of(b).list(who if mine else None, tag)}

    @app.get("/v1/watchlist/capacity", tags=["watchlist"])
    async def watch_capacity(b: Svc) -> dict:
        return await watch_of(b).capacity_report()

    @app.get("/v1/watchlist/{item_id}", tags=["watchlist"])
    async def watch_get(b: Svc, who: Consumer, item_id: int) -> dict:
        item = await watch_of(b).get(item_id)
        if item is None:
            raise NotFound(f"watch item {item_id} not found")
        return {"data": item}

    @app.patch("/v1/watchlist/{item_id}", tags=["watchlist"])
    async def watch_patch(b: Svc, who: Consumer, item_id: int, body: WatchPatch) -> dict:
        item = await watch_of(b).patch(item_id, body.interval_sec, body.tags, body.enabled)
        if item is None:
            raise NotFound(f"watch item {item_id} not found")
        return {"data": item}

    @app.delete("/v1/watchlist/{item_id}", tags=["watchlist"])
    async def watch_delete(b: Svc, who: Consumer, item_id: int) -> dict:
        action = await watch_of(b).delete(item_id, who)
        if action is None:
            raise NotFound(f"watch item {item_id} not found")
        return {"action": action}

    @app.get("/v1/feed", tags=["feed"])
    async def feed(
        b: Svc,
        who: Consumer,
        tags: Annotated[str | None, Query(max_length=400, description="comma-separated; any match")] = None,
        since: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        wait_sec: Annotated[float, Query(ge=0, le=30)] = 0,
    ) -> dict:
        tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()] if tags else None
        return await watch_of(b).read_feed(since, tag_list, limit, wait_sec)

    return app


def _check_username(username: str) -> None:
    if not re.fullmatch(USERNAME_RE, username):
        raise ApiError(400, "invalid_parameter", f"username: {username!r} is not a valid X username")
