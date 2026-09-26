"""Build GraphQL requests for an operation in a given limit bucket (spec §5 buckets, §6)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from xscout.transport.session import PreparedRequest
from xscout.xweb.constants import BEARERS, GQL_URL, Bucket
from xscout.xweb.registry import OpSpec
from xscout.xweb.tid import TidGenerator


class TidRequired(Exception):
    """The bucket needs a transaction id and no TID layer is available."""


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def base_headers(bucket: Bucket, language: str = "en") -> dict[str, str]:
    headers = {
        "authorization": BEARERS[bucket.bearer],
        "content-type": "application/json",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": language,
        "referer": "https://x.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if bucket.method == "POST":
        headers["origin"] = "https://x.com"
    return headers


def build_request(
    spec: OpSpec,
    variables: dict[str, Any],
    bucket: Bucket,
    tid: TidGenerator | None,
    language: str = "en",
) -> PreparedRequest:
    url = f"{GQL_URL}/{spec.query_id}/{spec.name}"
    headers = base_headers(bucket, language)
    if bucket.sends_tid and tid is not None:
        headers["x-client-transaction-id"] = tid.calc(bucket.method, urlparse(url).path)
    elif bucket.needs_tid:
        raise TidRequired(f"{spec.name} {bucket} needs a transaction id")
    if bucket.method == "GET":
        params = {"variables": _compact(variables), "features": _compact(spec.features)}
        if spec.field_toggles:
            params["fieldToggles"] = _compact(spec.field_toggles)
        return PreparedRequest("GET", url, params=params, headers=headers)
    payload: dict[str, Any] = {"variables": variables, "features": spec.features, "queryId": spec.query_id}
    if spec.field_toggles:
        payload["fieldToggles"] = spec.field_toggles
    return PreparedRequest("POST", url, body=_compact(payload), headers=headers)
