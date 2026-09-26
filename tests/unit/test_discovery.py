from pathlib import Path

import pytest

from xscout.xweb.discovery import DiscoveryError, discover, extract_ops, feature_config, script_urls

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "x"
MAIN_URL = "https://abs.twimg.com/responsive-web/client-web/main.941731a8bedadd89a.js"
CHUNK_URL = "https://abs.twimg.com/responsive-web/client-web/chunk.TweetDetail.abcdef0123456789a.js"

HOME = (
    '<html><head><meta name="twitter-site-verification" content="x"/></head><body>'
    f'<script src="{MAIN_URL}"></script>'
    '<script>window.__INITIAL_STATE__={"featureSwitch":{"defaultConfig":{"rweb_video_screen_enabled":{"value":false},'
    '"rweb_cashtags_enabled":{"value":true},"view_counts_everywhere_api_enabled":{"value":true}},'
    '"user":{"config":{"verified_phone_label_enabled":{"value":false}}}}};window.__META_DATA__={};</script>'
    '<script>var m={85:"c340ed7eb9a2188b",86:"Settings"};</script>'
    "</body></html>"
)


def test_script_urls_include_main_and_hash_map_chunks():
    urls = script_urls(HOME)
    assert MAIN_URL in urls
    assert "https://abs.twimg.com/responsive-web/client-web/85.c340ed7eb9a2188ba.js" in urls


def test_script_urls_detects_cloudflare_interstitial():
    with pytest.raises(DiscoveryError):
        script_urls('<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>')


def test_feature_config_merges_default_and_user():
    cfg = feature_config(HOME)
    assert cfg == {
        "rweb_video_screen_enabled": False,
        "rweb_cashtags_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "verified_phone_label_enabled": False,
    }


def test_extract_ops_both_patterns():
    legacy = extract_ops((FIX / "bundle_main.js").read_text())
    assert legacy["SearchTimeline"].query_id == "auLkqtmHqYEpRvflfvLhyQ"
    assert legacy["SearchTimeline"].feature_switches[-1] == "rweb_conversational_replies_downvote_enabled"
    assert legacy["UserByScreenName"].field_toggles == ["withPayments", "withAuxiliaryUserLabels"]
    xweb = extract_ops((FIX / "bundle_chunk_xweb.js").read_text())
    assert xweb["TweetDetail"].query_id == "zoF7_t363wZyzylk-BLfZQ"
    assert xweb["TweetDetail"].feature_switches == ["view_counts_everywhere_api_enabled"]


async def test_discover_walks_main_then_imports_and_resolves_features():
    pages = {
        "https://x.com/home": ("text/html", HOME),
        MAIN_URL: ("application/javascript", (FIX / "bundle_main.js").read_text()),
        CHUNK_URL: ("application/javascript", (FIX / "bundle_chunk_xweb.js").read_text()),
    }
    calls: list[str] = []

    async def fetch(url: str):
        calls.append(url)
        if url in pages:
            return 200, pages[url][0], pages[url][1]
        return 404, "text/html", ""

    result = await discover(fetch)
    assert result.missing == []
    assert result.build == "responsive-web"
    assert calls[1] == MAIN_URL  # main bundle first
    features = result.resolved_features("SearchTimeline")
    # a switch missing from the page config resolves to false, as the web client does
    assert features == {
        "rweb_video_screen_enabled": False,
        "rweb_cashtags_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "rweb_conversational_replies_downvote_enabled": False,
    }


async def test_discover_rejects_logged_out_app():
    html = '<script src="https://abs.twimg.com/x-web/assets/entry-client-logged-out-abc.js"></script>'

    async def fetch(url: str):
        return 200, "text/html", html

    with pytest.raises(DiscoveryError):
        await discover(fetch)
