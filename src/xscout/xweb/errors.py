"""Extract X error codes from any response, including errors inside HTTP 200 (phase 0: code 214)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from xscout.transport.session import RawResponse


@dataclass(frozen=True)
class XError:
    code: int | None
    message: str


@dataclass(frozen=True)
class RateInfo:
    limit: int | None
    remaining: int | None
    reset_at: datetime | None


@dataclass(frozen=True)
class Inspected:
    """What the gateway needs to classify a response."""

    status: int
    body: object | None  # parsed JSON, None when the body is not JSON
    errors: list[XError]
    rate: RateInfo
    is_html: bool
    has_data: bool

    @property
    def codes(self) -> list[int]:
        return [e.code for e in self.errors if e.code is not None]


def _int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def rate_info(headers: dict[str, str]) -> RateInfo:
    reset = _int(headers.get("x-rate-limit-reset"))
    return RateInfo(
        limit=_int(headers.get("x-rate-limit-limit")),
        remaining=_int(headers.get("x-rate-limit-remaining")),
        reset_at=datetime.fromtimestamp(reset, UTC) if reset else None,
    )


def inspect(rep: RawResponse) -> Inspected:
    body: object | None = None
    try:
        body = json.loads(rep.text) if rep.text else None
    except ValueError:
        body = None
    errors: list[XError] = []
    has_data = False
    if isinstance(body, dict):
        for e in body.get("errors") or []:
            if isinstance(e, dict):
                code = e.get("code")
                errors.append(
                    XError(
                        int(code) if isinstance(code, int | str) and str(code).isdigit() else None,
                        str(e.get("message", "")),
                    )
                )
        data = body.get("data")
        has_data = isinstance(data, dict) and any(v is not None for v in data.values())
    text_start = rep.text.lstrip()[:200].lower()
    is_html = body is None and ("text/html" in rep.content_type or text_start.startswith(("<!doctype", "<html")))
    return Inspected(rep.status, body, errors, rate_info(rep.headers), is_html, has_data)
