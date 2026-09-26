import json
from pathlib import Path

from xscout.transport.session import PreparedRequest, RawResponse
from xscout.xweb import ops
from xscout.xweb.client import AccountClient
from xscout.xweb.constants import Bucket
from xscout.xweb.registry import OperationRegistry, load_fallback, parse_missing_features
from xscout.xweb.tid import TidGenerator

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "x"


def test_fallback_has_v1_operations_with_features():
    specs = load_fallback()
    for name in ("SearchTimeline", "UserByScreenName", "UserTweets"):
        assert specs[name].query_id and len(specs[name].features) >= 10 and specs[name].source == "fallback"
    assert specs["SearchTimeline"].field_toggles == {"withArticleRichContentState": True}


def test_parse_missing_features():
    msg = "The following features cannot be null: a_enabled, b_enabled"
    assert parse_missing_features(msg) == ["a_enabled", "b_enabled"]
    assert parse_missing_features("something else") == []


async def test_heal_missing_features_once():
    reg = OperationRegistry()
    msg = "features cannot be null: brand_new_flag"
    assert await reg.heal_missing_features("SearchTimeline", msg) is True
    assert reg.get("SearchTimeline").features["brand_new_flag"] is True
    assert await reg.heal_missing_features("SearchTimeline", msg) is False


async def test_refresh_failure_keeps_specs():
    reg = OperationRegistry()
    before = reg.get("SearchTimeline")

    async def fetch(url):
        return 500, "text/html", ""

    assert await reg.refresh(fetch) == []
    assert reg.get("SearchTimeline") == before and "discovery failed" in reg.last_error


class FakeTransport:
    account_id = 1

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent: list[PreparedRequest] = []

    async def send(self, req):
        self.sent.append(req)
        status, body = self.responses.pop(0)
        return RawResponse(status, {"content-type": "application/json"}, body, 3, req.url)


class FakeTids:
    def __init__(self):
        self.invalidated = 0

    async def get(self, account_id, page, fetch):
        return TidGenerator(list(range(48)), "abc", "generator")

    def invalidate(self, account_id):
        self.invalidated += 1


async def test_client_heals_336_and_parses():
    good = (FIX / "search_latest_post.json").read_text()
    err = json.dumps({"errors": [{"code": 336, "message": "features cannot be null: new_flag"}]})
    t = FakeTransport([(400, err), (200, good)])
    client = AccountClient(t, OperationRegistry(), FakeTids())
    out = await client.call(ops.search("bitcoin"), Bucket.parse("POST/main"))
    assert out.ok and out.attempts == 2 and len(out.parsed.items) == 20
    assert json.loads(t.sent[1].body)["features"]["new_flag"] is True


async def test_client_rebuilds_tid_once_on_404():
    tids = FakeTids()
    t = FakeTransport([(404, ""), (404, "")])
    out = await AccountClient(t, OperationRegistry(), tids).call(ops.search("x"), Bucket.parse("GET/main"))
    assert not out.ok and out.attempts == 2 and tids.invalidated == 1


async def test_client_stores_sample_on_parse_failure():
    samples = []

    async def sink(**kw):
        samples.append(kw)

    broken = json.dumps({"data": {"user": {"result": {"rest_id": "1", "core": 5}}}})
    t = FakeTransport([(200, broken)])
    client = AccountClient(t, OperationRegistry(), FakeTids(), samples=sink)
    call = ops.OpCall("UserByScreenName", {}, lambda body: 1 / 0, paginated=False)
    out = await client.call(call, Bucket.parse("GET/main"))
    assert out.parse_error and samples and samples[0]["operation"] == "UserByScreenName"
