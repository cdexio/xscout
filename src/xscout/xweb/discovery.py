"""Operation and feature discovery from x.com pages and JS bundles (spec §6, plan 2.2).

Pure extraction functions plus one async `discover()` that fetches through any callable that
returns page text, so it can run through an account transport or be tested with fixtures.
Script-list parsing follows twscrape's `xclid.get_scripts_list` (MIT, see THIRD_PARTY_NOTICES.md).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

from xscout.xweb.constants import HOME_URL, WANTED_OPS

FetchText = Callable[[str], Awaitable[tuple[int, str, str]]]  # url -> (status, content_type, text)


class DiscoveryError(Exception):
    pass


ASSET_URL_RE = re.compile(r"https://[\w.-]+/x-web/[\w./-]+\.js")
RESPONSIVE_WEB_URL_RE = re.compile(r"https://[\w.-]+/responsive-web/client-web/[\w./-]+\.js")
LEGACY_MAIN_RE = re.compile(r"/client-web/main\.([^.\"']+)\.js")
LOGGED_OUT_ENTRY_RE = re.compile(r"(?:^|/)entry-client-logged-out(?:[-.][^/?#]+)?\.js(?:[?#].*)?$")
CHALLENGE_SCRIPT_RE = re.compile(r"/cdn-cgi/challenge-platform/(?:scripts|h)/")
JS_REF_RE = re.compile(r'(?:from|import)\s*\(?\s*[`"]((?:\.{1,2}/)[^`"]+?\.js)[`"]')
HEX_HASH = r"[0-9a-f]{7}|[0-9a-f]{16}"

OP_PATTERNS = (
    re.compile(r'queryId:[`"]([^`"]+)[`"].{0,200}?operationName:[`"]([^`"]+)[`"]', re.S),
    re.compile(r'params:\{id:[`"]([^`"]+)[`"].{0,300}?name:[`"]([^`"]+)[`"].{0,200}?operationKind:[`"]', re.S),
)
FEATURE_SWITCHES_RE = re.compile(r"featureSwitches:\[([^\]]*)\]")
FIELD_TOGGLES_RE = re.compile(r"fieldToggles:\[([^\]]*)\]")
QUOTED_RE = re.compile(r'"([^"]+)"')
INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__=(\{.*?\});window\.__META_DATA__", re.S)


@dataclass
class DiscoveredOp:
    name: str
    query_id: str
    feature_switches: list[str] | None = None
    field_toggles: list[str] | None = None
    source: str = ""


@dataclass
class DiscoveryResult:
    ops: dict[str, DiscoveredOp]
    feature_config: dict[str, object]
    scripts_fetched: int
    build: str  # "responsive-web" or "x-web"
    missing: list[str] = field(default_factory=list)

    def resolved_features(self, name: str) -> dict[str, object] | None:
        op = self.ops.get(name)
        if op is None or op.feature_switches is None:
            return None
        return {k: self.feature_config.get(k, False) for k in op.feature_switches}


def page_is_logged_out(scripts: list[str]) -> bool:
    return any(LOGGED_OUT_ENTRY_RE.search(u) for u in scripts)


def script_urls(html: str) -> list[str]:
    urls = ASSET_URL_RE.findall(html) + RESPONSIVE_WEB_URL_RE.findall(html)
    if m := LEGACY_MAIN_RE.search(html):
        urls.append(f"https://abs.twimg.com/responsive-web/client-web/main.{m.group(1)}.js")
    if not urls and CHALLENGE_SCRIPT_RE.search(html):
        raise DiscoveryError("Cloudflare challenge page served instead of the X web app")
    hashes = {m.group(1): m.group(2) for m in re.finditer(rf'(\d+):"({HEX_HASH})"', html)}
    names = {
        m.group(1): m.group(2) for m in re.finditer(r'(\d+):"([^"]+)"', html) if not re.fullmatch(HEX_HASH, m.group(2))
    }
    urls.extend(
        f"https://abs.twimg.com/responsive-web/client-web/{names.get(cid, cid)}.{h}a.js" for cid, h in hashes.items()
    )
    urls = [u for u in dict.fromkeys(urls) if "/i18n/" not in u and "/icons/" not in u]
    if not urls:
        raise DiscoveryError("no X web scripts found in page")
    return urls


def feature_config(html: str) -> dict[str, object]:
    """Feature switch defaults embedded in the page (`window.__INITIAL_STATE__.featureSwitch`)."""
    m = INITIAL_STATE_RE.search(html)
    if not m:
        return {}
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    fs = state.get("featureSwitch", {}) if isinstance(state, dict) else {}
    merged: dict[str, object] = {}
    for section in (fs.get("defaultConfig", {}), fs.get("user", {}).get("config", {})):
        for key, val in section.items():
            if isinstance(val, dict) and "value" in val:
                merged[key] = val["value"]
    return merged


def extract_ops(js: str, source: str = "") -> dict[str, DiscoveredOp]:
    ops: dict[str, DiscoveredOp] = {}
    for rx in OP_PATTERNS:
        for m in rx.finditer(js):
            query_id, name = m.group(1), m.group(2)
            if name in ops:
                continue
            window = js[m.start() : m.start() + 3000]
            fs = FEATURE_SWITCHES_RE.search(window)
            ft = FIELD_TOGGLES_RE.search(window)
            ops[name] = DiscoveredOp(
                name=name,
                query_id=query_id,
                feature_switches=QUOTED_RE.findall(fs.group(1)) if fs else None,
                field_toggles=QUOTED_RE.findall(ft.group(1)) if ft else None,
                source=source,
            )
    return ops


async def discover(fetch: FetchText, wanted: tuple[str, ...] = WANTED_OPS, max_scripts: int = 400) -> DiscoveryResult:
    """Fetch the logged-in home page and walk bundles (main first) until every wanted op is found."""
    status, _, html = await fetch(HOME_URL)
    if status >= 400:
        raise DiscoveryError(f"home page returned HTTP {status}")
    scripts = script_urls(html)
    if page_is_logged_out(scripts):
        raise DiscoveryError("x.com served the logged-out app; cookies are not accepted")
    build = "x-web" if any("/x-web/" in u for u in scripts) else "responsive-web"
    config = feature_config(html)
    queue = sorted(scripts, key=lambda u: 0 if "/main." in u else 1 if "/x-web/" in u else 2)
    seen = set(queue)
    ops: dict[str, DiscoveredOp] = {}
    fetched = 0
    sem = asyncio.Semaphore(8)

    async def fetch_js(url: str) -> tuple[str, str | None]:
        async with sem:
            try:
                st, ctype, text = await fetch(url)
            except Exception:
                return url, None
            return url, text if st < 400 and "javascript" in ctype else None

    while queue and fetched < max_scripts and not all(w in ops for w in wanted):
        batch, queue = (queue[:1], queue[1:]) if fetched == 0 else (queue[:40], queue[40:])
        results = await asyncio.gather(*(fetch_js(u) for u in batch))
        fetched += len(batch)
        for url, text in results:
            if text is None:
                continue
            for name, op in extract_ops(text, url.rsplit("/", 1)[-1]).items():
                ops.setdefault(name, op)
            for ref in JS_REF_RE.findall(text):
                full = urljoin(url, ref)
                if full not in seen:
                    seen.add(full)
                    queue.append(full)
    return DiscoveryResult(
        ops=ops,
        feature_config=config,
        scripts_fetched=fetched,
        build=build,
        missing=[w for w in wanted if w not in ops],
    )
