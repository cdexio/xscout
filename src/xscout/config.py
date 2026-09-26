"""Settings: secrets and endpoints from env/.env, tunables from a YAML file (spec §5, §12)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[2]

Bucket = Literal["POST/main", "GET/main", "POST/alt", "GET/alt"]


class PoolTunables(BaseModel):
    reserve_min: int = 5
    reserve_ratio: float = Field(0.10, ge=0, lt=1)
    gap_min_sec: float = 2.0
    gap_max_sec: float = 4.0
    max_in_flight_per_account: int = Field(1, ge=1)
    lease_timeout_sec: float = 60.0
    impersonate_profiles: list[str] = Field(default_factory=lambda: ["chrome150", "chrome146", "chrome145"])

    @model_validator(mode="after")
    def _gap_order(self) -> PoolTunables:
        if self.gap_max_sec < self.gap_min_sec:
            raise ValueError("pool.gap_max_sec must be >= pool.gap_min_sec")
        return self


class CooldownTunables(BaseModel):
    rate_limited_sec: int = 3600
    rate_limited_lock_after: int = 3
    rate_limited_lock_window_sec: int = 86400
    cloudflare_sec: int = 900
    unknown_error_sec: int = 900


class BudgetTunables(BaseModel):
    p0_share: float = Field(0.4, ge=0, le=1)
    p1_share: float = Field(0.6, ge=0, le=1)

    @model_validator(mode="after")
    def _shares_sum(self) -> BudgetTunables:
        if abs(self.p0_share + self.p1_share - 1.0) > 1e-9:
            raise ValueError("budget.p0_share + budget.p1_share must equal 1.0")
        return self


class BucketTunables(BaseModel):
    """Ordered allowed buckets per operation; overflow buckets are used only when the others are full."""

    order: dict[str, list[Bucket]] = Field(
        default_factory=lambda: {
            "SearchTimeline": ["POST/main", "GET/main", "POST/alt", "GET/alt"],
            "UserByScreenName": ["GET/main", "GET/alt"],
            "UserTweets": ["GET/main", "GET/alt"],
        }
    )
    overflow: list[Bucket] = Field(default_factory=lambda: ["POST/alt", "GET/alt"])
    overflow_enabled: bool = True
    # Conservative starting limits per 15 min until real headers are seen (phase 0 measurements).
    limit_defaults: dict[str, dict[str, int]] = Field(
        default_factory=lambda: {
            "SearchTimeline": {"GET/main": 50, "POST/main": 187, "GET/alt": 50, "POST/alt": 187},
            "UserByScreenName": {"GET/main": 150, "GET/alt": 150},
            "UserTweets": {"GET/main": 50, "GET/alt": 50},
        }
    )


class CacheTunables(BaseModel):
    ttl_sec: dict[str, int] = Field(
        default_factory=lambda: {"SearchTimeline": 60, "UserByScreenName": 3600, "UserTweets": 120}
    )
    default_ttl_sec: int = 60


class ApiTunables(BaseModel):
    page_size: int = 20
    max_pages: int = Field(10, ge=1)


class WatchTunables(BaseModel):
    max_query_chars: int = Field(500, le=512)
    max_pages_per_poll: int = 5


class RetentionTunables(BaseModel):
    tweets_days: int = 90
    samples_days: int = 7
    request_log_days: int = 30


class IntervalTunables(BaseModel):
    registry_refresh_sec: int = 21600
    canary_sec: int = 600
    tid_rebuild_sec: int = 10800


class Tunables(BaseModel):
    pool: PoolTunables = Field(default_factory=PoolTunables)
    cooldowns: CooldownTunables = Field(default_factory=CooldownTunables)
    budget: BudgetTunables = Field(default_factory=BudgetTunables)
    buckets: BucketTunables = Field(default_factory=BucketTunables)
    cache: CacheTunables = Field(default_factory=CacheTunables)
    api: ApiTunables = Field(default_factory=ApiTunables)
    watch: WatchTunables = Field(default_factory=WatchTunables)
    retention: RetentionTunables = Field(default_factory=RetentionTunables)
    intervals: IntervalTunables = Field(default_factory=IntervalTunables)

    @classmethod
    def from_yaml(cls, path: Path) -> Tunables:
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        return cls.model_validate(data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="XSCOUT_", env_file=PROJECT_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str
    secret_key: SecretStr
    host: str = "127.0.0.1"
    port: int = 8790
    config_file: Path = PROJECT_DIR / "config" / "xscout.yaml"
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("XSCOUT_DATABASE_URL must start with postgresql+asyncpg://")
        return v

    @field_validator("secret_key")
    @classmethod
    def _key_present(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().strip():
            raise ValueError("XSCOUT_SECRET_KEY is empty; generate one with `xscout keygen`")
        return v

    @property
    def tunables(self) -> Tunables:
        return _load_tunables(self.config_file)


@lru_cache(maxsize=4)
def _load_tunables(path: Path) -> Tunables:
    return Tunables.from_yaml(path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from env
