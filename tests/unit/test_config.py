from pathlib import Path

import pytest
from pydantic import ValidationError

from xscout.config import Settings, Tunables

KEY = "k" * 43 + "="
PG = "postgresql+asyncpg://u:p@localhost:5432/xscout"


def test_defaults_match_spec():
    t = Tunables()
    assert t.budget.p0_share == 0.4 and t.budget.p1_share == 0.6
    assert t.buckets.order["SearchTimeline"][0] == "POST/main"
    assert t.buckets.overflow == ["POST/alt", "GET/alt"]
    assert t.buckets.limit_defaults["SearchTimeline"]["POST/main"] == 187
    assert t.watch.max_query_chars <= 512


def test_yaml_overrides_are_partial(tmp_path: Path):
    f = tmp_path / "x.yaml"
    f.write_text("pool:\n  reserve_min: 8\nbuckets:\n  overflow_enabled: false\n")
    t = Tunables.from_yaml(f)
    assert t.pool.reserve_min == 8
    assert t.pool.reserve_ratio == 0.10
    assert t.buckets.overflow_enabled is False


def test_missing_yaml_gives_defaults(tmp_path: Path):
    assert Tunables.from_yaml(tmp_path / "absent.yaml") == Tunables()


def test_example_yaml_is_valid():
    example = Path(__file__).resolve().parents[2] / "config" / "xscout.example.yaml"
    assert Tunables.from_yaml(example) == Tunables()


def test_budget_shares_must_sum_to_one(tmp_path: Path):
    f = tmp_path / "x.yaml"
    f.write_text("budget:\n  p0_share: 0.5\n  p1_share: 0.6\n")
    with pytest.raises(ValidationError):
        Tunables.from_yaml(f)


def test_unknown_bucket_rejected(tmp_path: Path):
    f = tmp_path / "x.yaml"
    f.write_text("buckets:\n  overflow: [PUT/alt]\n")
    with pytest.raises(ValidationError):
        Tunables.from_yaml(f)


def test_settings_require_async_driver_and_key(monkeypatch):
    monkeypatch.setenv("XSCOUT_DATABASE_URL", "postgresql://u:p@localhost/xscout")
    monkeypatch.setenv("XSCOUT_SECRET_KEY", KEY)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("XSCOUT_DATABASE_URL", PG)
    monkeypatch.setenv("XSCOUT_SECRET_KEY", "  ")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("XSCOUT_SECRET_KEY", KEY)
    s = Settings(_env_file=None)
    assert s.host == "127.0.0.1" and s.port == 8790
