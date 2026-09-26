import json

import pytest

from xscout.transport.session import RawResponse
from xscout.xweb import ops
from xscout.xweb.constants import BEARERS, Bucket
from xscout.xweb.errors import inspect
from xscout.xweb.registry import OpSpec
from xscout.xweb.requests import TidRequired, build_request
from xscout.xweb.tid import TidGenerator

SPEC = OpSpec("SearchTimeline", "qid123", {"f1": True}, {"withArticleRichContentState": True}, "test")
TID = TidGenerator(list(range(48)), "abc", "test")
VARS = ops.search("bitcoin").variables


def test_bucket_parse_and_rules():
    assert Bucket.parse("POST/main").name == "POST/main"
    with pytest.raises(ValueError):
        Bucket.parse("PUT/main")
    assert Bucket.parse("GET/main").needs_tid and not Bucket.parse("POST/main").needs_tid
    assert not Bucket.parse("GET/alt").sends_tid


def test_get_request_shape_and_tid():
    req = build_request(SPEC, VARS, Bucket.parse("GET/main"), TID)
    assert req.method == "GET" and req.url.endswith("/qid123/SearchTimeline")
    assert json.loads(req.params["variables"])["rawQuery"] == "bitcoin"
    assert json.loads(req.params["features"]) == {"f1": True}
    assert json.loads(req.params["fieldToggles"]) == {"withArticleRichContentState": True}
    assert req.headers["authorization"] == BEARERS["main"]
    assert "x-client-transaction-id" in req.headers and req.body is None


def test_get_main_without_tid_is_refused():
    with pytest.raises(TidRequired):
        build_request(SPEC, VARS, Bucket.parse("GET/main"), None)


def test_post_request_body_and_alt_bearer_sends_no_tid():
    req = build_request(SPEC, VARS, Bucket.parse("POST/alt"), TID)
    body = json.loads(req.body)
    assert body["queryId"] == "qid123" and body["variables"]["product"] == "Latest"
    assert req.headers["authorization"] == BEARERS["alt"]
    assert req.headers["origin"] == "https://x.com"
    assert "x-client-transaction-id" not in req.headers
    assert build_request(SPEC, VARS, Bucket.parse("POST/main"), None).method == "POST"


def test_search_query_limits():
    with pytest.raises(ops.QueryTooLong):
        ops.search("x" * 513)
    with pytest.raises(ValueError):
        ops.search("   ")
    assert ops.search("a", "people").variables["product"] == "People"


def rep(status=200, text="", headers=None, ctype="application/json"):
    return RawResponse(status, {"content-type": ctype, **(headers or {})}, text, 5, "https://x.com/")


def test_inspect_error_inside_http_200():
    body = {
        "errors": [{"code": 214, "message": "BadRequest: Raw query length 984 exceeds max allowed 512"}],
        "data": {},
    }
    ins = inspect(
        rep(
            text=json.dumps(body),
            headers={"x-rate-limit-limit": "50", "x-rate-limit-remaining": "34", "x-rate-limit-reset": "1790000000"},
        )
    )
    assert ins.status == 200 and ins.codes == [214] and not ins.has_data
    assert (ins.rate.limit, ins.rate.remaining) == (50, 34) and ins.rate.reset_at.year == 2026


def test_inspect_html_and_empty_404():
    assert inspect(rep(403, "<!DOCTYPE html><title>Attention Required</title>", ctype="text/html")).is_html
    ins = inspect(rep(404, "", ctype=""))
    assert ins.body is None and not ins.is_html and ins.errors == []


def test_inspect_data_present():
    ins = inspect(rep(text=json.dumps({"data": {"user": {"result": {}}}})))
    assert ins.has_data and ins.errors == []
