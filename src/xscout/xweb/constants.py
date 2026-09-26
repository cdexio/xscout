"""Wire-level constants of the X web client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GQL_URL = "https://x.com/i/api/graphql"
HOME_URL = "https://x.com/home"
# Responsive-web app shell. Upstream (Lqm1/x-client-transaction-id 2f1f95b, 2026-09-25) moved to it after
# logged-out /home started redirecting to the x-web build; both served TID inputs to our session on 2026-09-26.
APP_SHELL_URL = "https://x.com/i/jf/"
TID_PAGES = (HOME_URL, APP_SHELL_URL)

# Public web-client bearer tokens (same values in twscrape, twikit and Nitter). "alt" is the
# second token Nitter uses; phase 0 measured separate limit buckets for it.
BEARERS = {
    "main": (
        "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
        "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
    ),
    "alt": (
        "Bearer AAAAAAAAAAAAAAAAAAAAAFXzAwAAAAAAMHCxpeSDG1gLNLghVe8d74hl6k4"
        "%3DRUMF4xAQLsbeBhTSRrCiQpJtxoGWeyHrDb5te2jpGskWDFW82F"
    ),
}

# Operations xscout uses in v1 (discovery keeps looking until these are found).
WANTED_OPS = ("SearchTimeline", "UserByScreenName", "UserTweets", "UserByRestId", "TweetDetail")

# Field toggle values sent per operation; the combinations below were verified live in phase 0.
FIELD_TOGGLES: dict[str, dict[str, bool]] = {
    "SearchTimeline": {"withArticleRichContentState": True},
    "UserTweets": {"withArticleRichContentState": True},
    "TweetDetail": {"withArticleRichContentState": True},
    "UserByScreenName": {"withAuxiliaryUserLabels": False},
    "UserByRestId": {"withAuxiliaryUserLabels": False},
}

Method = Literal["GET", "POST"]
BearerName = Literal["main", "alt"]


@dataclass(frozen=True)
class Bucket:
    """A limit bucket: X counts quota separately per HTTP method and bearer token."""

    method: Method
    bearer: BearerName

    @staticmethod
    def parse(value: str) -> Bucket:
        method, _, bearer = value.partition("/")
        if method not in ("GET", "POST") or bearer not in ("main", "alt"):
            raise ValueError(f"invalid bucket {value!r}; expected e.g. POST/main")
        return Bucket(method, bearer)  # type: ignore[arg-type]

    @property
    def name(self) -> str:
        return f"{self.method}/{self.bearer}"

    @property
    def needs_tid(self) -> bool:
        # Phase 0: GET with the main bearer returns 404 without a transaction id.
        return self.method == "GET" and self.bearer == "main"

    @property
    def sends_tid(self) -> bool:
        # The web client sends a TID with the main bearer; Nitter's alt-bearer path sends none.
        return self.bearer == "main"

    def __str__(self) -> str:
        return self.name
