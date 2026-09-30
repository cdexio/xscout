"""Per-tag content filters for watch items (owner request 2026-09-30, option B).

Filters are applied after a poll, so batching and X request counts do not change. An item can serve
several bots; each bot's tag carries its own filter, and a tweet reaches the feed only with the tags
whose filter it passes.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from xscout.xweb.models import Tweet

CASHTAG_RE = re.compile(r"(?<![\w$])\$[A-Za-z][A-Za-z0-9_]{0,14}\b")
SOLANA_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


class WatchFilter(BaseModel):
    exclude_replies: bool = False
    exclude_retweets: bool = False
    require_cashtag: bool = False
    require_contract: bool = False  # a Solana or EVM address in the text or a link
    keywords: list[str] = Field(default_factory=list, max_length=30)  # any match, case-insensitive

    @field_validator("keywords")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = []
        for k in v:
            k = k.strip().lower()
            if not k or len(k) > 60:
                raise ValueError("keywords must be 1-60 characters")
            out.append(k)
        return sorted(set(out))

    def is_noop(self) -> bool:
        return self == WatchFilter()


def has_contract(text: str) -> bool:
    return bool(EVM_ADDRESS_RE.search(text) or any(_looks_like_solana(m) for m in SOLANA_ADDRESS_RE.findall(text)))


def _looks_like_solana(candidate: str) -> bool:
    # Base58 runs of 32-44 characters also match long words; real addresses mix digits and both cases.
    return (
        any(c.isdigit() for c in candidate)
        and any(c.isupper() for c in candidate)
        and any(c.islower() for c in candidate)
    )


def passes(tweet: Tweet, spec: WatchFilter | None) -> bool:
    if spec is None:
        return True
    if spec.exclude_replies and tweet.is_reply:
        return False
    if spec.exclude_retweets and tweet.is_retweet:
        return False
    text = tweet.text or ""
    haystack = " ".join([text, *tweet.urls])
    if spec.require_cashtag and not (tweet.cashtags or CASHTAG_RE.search(text)):
        return False
    if spec.require_contract and not has_contract(haystack):
        return False
    if spec.keywords:
        lowered = haystack.lower()
        if not any(k in lowered for k in spec.keywords):
            return False
    return True


def passing_tags(tweet: Tweet, tags: list[str], filters: dict[str, dict]) -> list[str]:
    """Tags of an item that this tweet satisfies. Tags without a filter always pass."""
    return [t for t in tags if passes(tweet, WatchFilter.model_validate(filters[t]) if t in filters else None)]
