import pytest

from xscout.store.accounts import (
    AccountError,
    normalize_username,
    parse_cookie_string,
    pick_impersonate,
    validate_cookie,
)

PROFILES = ["chrome150", "chrome146", "chrome145"]


def test_normalize_username():
    assert normalize_username(" @SomeUser_1 ") == "someuser_1"
    for bad in ("", "has space", "a" * 16, "semi;colon"):
        with pytest.raises(AccountError):
            normalize_username(bad)


def test_parse_cookie_string_from_browser_copy():
    raw = "auth_token=abc123abc123abc123abc1; ct0=def456def456def456def4;other=1"
    assert parse_cookie_string(raw) == {
        "auth_token": "abc123abc123abc123abc1",
        "ct0": "def456def456def456def4",
        "other": "1",
    }


def test_validate_cookie():
    assert validate_cookie("ct0", ' "abcdefabcdefabcdefab" ') == "abcdefabcdefabcdefab"
    for bad in ("short", "has space in it 1234567890", "x" * 600):
        with pytest.raises(AccountError):
            validate_cookie("ct0", bad)


def test_impersonate_profile_is_stable_and_spread():
    assert pick_impersonate("alice", PROFILES) == pick_impersonate("alice", PROFILES)
    picked = {pick_impersonate(f"user{i}", PROFILES) for i in range(30)}
    assert picked == set(PROFILES)
    with pytest.raises(AccountError):
        pick_impersonate("alice", [])
