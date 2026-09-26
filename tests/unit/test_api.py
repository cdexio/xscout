import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xscout.api.app import Backend, create_app
from xscout.api.service import XService
from xscout.config import Tunables
from xscout.gateway.gateway import BadRequest, Meta, Result, Unavailable
from xscout.xweb.parse import parse_tweet_timeline, parse_user_result, parse_user_timeline

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "x"
PAGE = parse_tweet_timeline(json.loads((FIX / "search_latest_post.json").read_text()))
PAGE2 = parse_tweet_timeline(json.loads((FIX / "search_latest.json").read_text()))
PEOPLE = parse_user_timeline(json.loads((FIX / "search_people.json").read_text()))
PROFILE = parse_user_result(json.loads((FIX / "user_by_screen_name.json").read_text()))
UT = parse_tweet_timeline(json.loads((FIX / "user_tweets_p2.json").read_text()))


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.fail: Exception | None = None

    async def run(self, make_call, *, pages=1, priority=None, consumer="?", cursor=None, max_age_sec=None):
        call = make_call(cursor)
        self.calls.append((call.operation, call.variables, pages, consumer, max_age_sec))
        if self.fail:
            raise self.fail
        meta = Meta(fetched_at=datetime(2026, 9, 26, tzinfo=UTC), pages_fetched=pages)
        if call.operation == "SearchTimeline":
            if call.variables["product"] == "People":
                return Result([PEOPLE], meta)
            return Result([PAGE, PAGE2][:pages], meta)
        if call.operation == "UserByScreenName":
            return Result([PROFILE if call.variables["screen_name"].lower() == "xdevelopers" else None], meta)
        return Result([UT], meta)


class Status:
    def health_report(self):
        return {"status": "ok"}

    def accounts_report(self):
        return [{"username": "a"}]

    def budget_report(self):
        return {"window_sec": 900}


@pytest.fixture
def env():
    gw = FakeGateway()
    archived = []

    async def archive(tweets, users):
        archived.append((len(tweets), len(users)))

    svc = XService(gw, Tunables(), archive=archive)

    @asynccontextmanager
    async def backend():
        yield Backend(service=svc, status=Status())

    with TestClient(create_app(backend)) as client:
        yield client, gw, archived


H = {"X-Consumer": "zetryn"}


def test_consumer_header_required(env):
    client, _, _ = env
    r = client.get("/v1/search/tweets", params={"q": "btc"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_consumer"
    assert client.get("/v1/search/tweets", params={"q": "btc"}, headers={"X-Consumer": "Bad Name!"}).status_code == 400


def test_search_tweets_envelope_and_paging(env):
    client, gw, archived = env
    r = client.get("/v1/search/tweets", params={"q": "$BTC", "limit": 30, "max_age_sec": 120}, headers=H)
    body = r.json()
    assert r.status_code == 200
    assert len(body["data"]) == 30 and body["next_cursor"] and "includes" in body
    assert body["meta"]["pages_fetched"] == 2 and body["meta"]["cached"] is False
    op, variables, pages, consumer, max_age = gw.calls[0]
    assert (op, variables["product"], pages, consumer, max_age) == ("SearchTimeline", "Latest", 2, "zetryn", 120)
    assert {"id", "text", "author", "created_at", "cashtags"} <= set(body["data"][0])


def test_limit_is_capped_by_max_pages(env):
    client, gw, _ = env
    client.get("/v1/search/tweets", params={"q": "x", "limit": 200}, headers=H)
    assert gw.calls[0][2] == Tunables().api.max_pages
    assert client.get("/v1/search/tweets", params={"q": "x", "limit": 201}, headers=H).status_code == 400


def test_search_users_and_profile(env):
    client, gw, _ = env
    users = client.get("/v1/search/users", params={"q": "solana", "limit": 5}, headers=H).json()
    assert len(users["data"]) == 5 and users["data"][0]["username"]
    prof = client.get("/v1/users/@XDevelopers", headers=H).json()
    assert prof["data"]["followers"] == 698326 and gw.calls[-1][1]["screen_name"] == "XDevelopers"
    missing = client.get("/v1/users/nobody_here", headers=H)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert client.get("/v1/users/bad-name!", headers=H).status_code == 400


def test_user_tweets_resolves_id_with_profile_cache(env):
    client, gw, _ = env
    r = client.get("/v1/users/XDevelopers/tweets", params={"limit": 5}, headers=H).json()
    assert len(r["data"]) == 5 and r["includes"]
    (op1, _, _, _, age1), (op2, v2, _, _, _) = gw.calls
    assert (op1, age1) == ("UserByScreenName", 3600)
    assert op2 == "UserTweets" and v2["userId"] == str(PROFILE.id)


def test_errors_map_to_status_codes(env):
    client, gw, _ = env
    gw.fail = Unavailable("no account with quota headroom", 42)
    r = client.get("/v1/search/tweets", params={"q": "x"}, headers=H)
    assert r.status_code == 503 and r.headers["retry-after"] == "42"
    error = r.json()["error"]
    assert (error["code"], error["message"], error["retry_after_sec"]) == ("unavailable", gw.fail.reason, 42)
    gw.fail = BadRequest("214: query too long")
    assert client.get("/v1/search/tweets", params={"q": "x"}, headers=H).json()["error"]["code"] == "bad_request"
    gw.fail = None
    too_long = client.get("/v1/search/tweets", params={"q": "x" * 513}, headers=H)
    assert too_long.status_code == 400 and too_long.json()["error"]["code"] == "invalid_parameter"


def test_status_endpoints(env):
    client, _, _ = env
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/v1/accounts").json() == [{"username": "a"}]
    assert client.get("/v1/budget").json() == {"window_sec": 900}


def test_openapi_lists_endpoints(env):
    client, _, _ = env
    paths = set(client.get("/openapi.json").json()["paths"])
    expected = {"/v1/search/tweets", "/v1/search/users", "/v1/users/{username}", "/v1/users/{username}/tweets"}
    assert expected | {"/health"} <= paths


async def test_service_archives_fresh_results_only():
    gw = FakeGateway()
    archived = []

    async def archive(tweets, users):
        archived.append((len(tweets), len(users)))

    svc = XService(gw, Tunables(), archive=archive)
    await svc.search_tweets("x", "latest", 20, "c")
    await svc.drain()
    assert archived and archived[0][0] >= 20

    class CachedGateway(FakeGateway):
        async def run(self, *a, **kw):
            r = await super().run(*a, **kw)
            r.meta.cached = True
            return r

    svc2 = XService(CachedGateway(), Tunables(), archive=archive)
    await svc2.search_tweets("x", "latest", 20, "c")
    await svc2.drain()
    assert len(archived) == 1
