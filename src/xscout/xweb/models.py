"""Stable, normalized models returned to the bots (spec §6). X's wire format never leaks past xweb."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class User(BaseModel):
    id: int
    username: str | None = None
    display_name: str | None = None
    bio: str | None = None
    created_at: datetime | None = None
    followers: int | None = None
    following: int | None = None
    tweet_count: int | None = None
    verified_type: str | None = None
    is_blue_verified: bool | None = None
    protected: bool | None = None
    location: str | None = None
    avatar_url: str | None = None
    url: str | None = None


class Media(BaseModel):
    type: str
    url: str
    video_url: str | None = None


class Tweet(BaseModel):
    id: int
    url: str | None = None
    text: str | None = None
    created_at: datetime | None = None
    lang: str | None = None
    author: User | None = None
    reply_count: int | None = None
    retweet_count: int | None = None
    quote_count: int | None = None
    like_count: int | None = None
    view_count: int | None = None
    bookmark_count: int | None = None
    is_reply: bool = False
    is_retweet: bool = False
    is_quote: bool = False
    is_pinned: bool = False
    in_reply_to_id: int | None = None
    quoted_id: int | None = None
    retweeted_id: int | None = None
    conversation_id: int | None = None
    hashtags: list[str] = Field(default_factory=list)
    cashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    media: list[Media] = Field(default_factory=list)


class ParseStats(BaseModel):
    entries: int = 0
    parsed: int = 0
    skipped: int = 0  # promoted, prompts, who-to-follow, tombstones
    failed: int = 0  # entries that looked like content but could not be parsed
    errors: list[str] = Field(default_factory=list)
    key_field_nulls: dict[str, int] = Field(default_factory=dict)

    def null_rate(self, field: str) -> float:
        return self.key_field_nulls.get(field, 0) / self.parsed if self.parsed else 0.0


class TweetPage(BaseModel):
    items: list[Tweet] = Field(default_factory=list)
    includes: list[Tweet] = Field(default_factory=list)  # quoted and retweeted originals
    cursor_bottom: str | None = None
    cursor_top: str | None = None
    stats: ParseStats = Field(default_factory=ParseStats)


class UserPage(BaseModel):
    items: list[User] = Field(default_factory=list)
    cursor_bottom: str | None = None
    cursor_top: str | None = None
    stats: ParseStats = Field(default_factory=ParseStats)
