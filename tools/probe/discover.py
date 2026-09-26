"""Phase 0.2 + 0.3: logged-in page check, queryId and feature discovery (no API quota used)."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urljoin

from common import OUT_DIR, Account, make_session, save_json
from xclid import LOGGED_OUT_ENTRY_RE, get_page_text, get_scripts_list

JS_REF_RE = re.compile(r'(?:from|import)\s*\(?\s*[`"]((?:\.{1,2}/)[^`"]+?\.js)[`"]')
OP_PATTERNS = {
    "queryId": re.compile(r'queryId:[`"]([^`"]+)[`"].{0,200}?operationName:[`"]([^`"]+)[`"]', re.S),
    "params": re.compile(r'params:\{id:[`"]([^`"]+)[`"].{0,300}?name:[`"]([^`"]+)[`"].{0,200}?operationKind:[`"]', re.S),
}
FEATURE_SWITCHES_RE = re.compile(r"featureSwitches:\[([^\]]*)\]")
FIELD_TOGGLES_RE = re.compile(r"fieldToggles:\[([^\]]*)\]")
WANTED_OPS = ["SearchTimeline", "UserByScreenName", "UserByRestId", "UserTweets", "TweetDetail"]
MAX_SCRIPTS = 400


def relevant(url: str) -> bool:
    return "/i18n/" not in url and "/icons/" not in url and "react-syntax-highlighter" not in url


def extract_feature_config(html: str) -> dict[str, object]:
    """Default feature switch values embedded in the page (window.__INITIAL_STATE__)."""
    m = re.search(r"window\.__INITIAL_STATE__=(\{.*?\});window\.__META_DATA__", html, re.S)
    if not m:
        return {}
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    fs = state.get("featureSwitch", {})
    merged: dict[str, object] = {}
    for section in (fs.get("defaultConfig", {}), fs.get("user", {}).get("config", {})):
        for key, val in section.items():
            if isinstance(val, dict) and "value" in val:
                merged[key] = val["value"]
    return merged


async def main() -> None:
    acc = Account.load()
    session = make_session(acc)
    report: dict = {}
    try:
        html = await get_page_text(session, "https://x.com/home")
        (OUT_DIR).mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "home.html").write_text(acc.redact(html))
        scripts = [u for u in get_scripts_list(html) if relevant(u)]
        report["page"] = {
            "bytes": len(html),
            "logged_out_entry": any(LOGGED_OUT_ENTRY_RE.search(u) for u in scripts),
            "has_verification_meta": 'name="twitter-site-verification"' in html,
            "has_anim_svg": "loading-x-anim" in html,
            "cloudflare_jsd_script": "/cdn-cgi/challenge-platform/" in html,
            "cloudflare_interstitial": "/cdn-cgi/challenge-platform/" in html and not scripts,
            "initial_scripts": len(scripts),
            "script_hosts": sorted({u.split("/")[2] + "/" + u.split("/")[3] for u in scripts}),
        }
        features = extract_feature_config(html)
        report["feature_config_count"] = len(features)

        sem = asyncio.Semaphore(8)
        bodies: dict[str, str] = {}

        async def fetch(url: str) -> None:
            async with sem:
                try:
                    rep = await session.get(url)
                    if rep.status_code < 400 and "javascript" in rep.headers.get("content-type", ""):
                        bodies[url] = rep.text
                except Exception as e:  # network noise is fine for a probe
                    print(f"  fetch failed {url}: {e}")

        def found_all() -> bool:
            return all(
                any(re.search(rf'[`"]{name}[`"]', body) for body in bodies.values()) for name in WANTED_OPS
            )

        # main.* first (legacy build keeps most operations there), then the rest in slices.
        ordered = sorted(scripts, key=lambda u: (0 if "/main." in u else 1 if "/x-web/" in u else 2))
        seen = set(scripts)
        queue = list(ordered)
        fetched = 0
        while queue and fetched < MAX_SCRIPTS:
            slice_, queue = queue[:1 if fetched == 0 else 40], queue[1 if fetched == 0 else 40:]
            await asyncio.gather(*(fetch(u) for u in slice_))
            fetched += len(slice_)
            for u in slice_:
                for ref in JS_REF_RE.findall(bodies.get(u, "")):
                    full = urljoin(u, ref)
                    if full not in seen and relevant(full):
                        seen.add(full)
                        queue.append(full)
            if found_all():
                break
        report["scripts_fetched"] = len(bodies)
        report["scripts_requested"] = fetched
        report["scripts_seen"] = len(seen)

        ops: dict[str, dict] = {}
        for url, text in bodies.items():
            for pattern_name, rx in OP_PATTERNS.items():
                for m in rx.finditer(text):
                    op_id, name = m.group(1), m.group(2)
                    entry = ops.setdefault(name, {"ids": {}, "patterns": set(), "sources": set()})
                    entry["ids"][op_id] = entry["ids"].get(op_id, 0) + 1
                    entry["patterns"].add(pattern_name)
                    entry["sources"].add(url.rsplit("/", 1)[-1])
                    if name in WANTED_OPS and "snippet" not in entry:
                        window = text[m.start(): m.start() + 3000]
                        fs = FEATURE_SWITCHES_RE.search(window)
                        ft = FIELD_TOGGLES_RE.search(window)
                        entry["snippet"] = window[:1500]
                        entry["feature_switches"] = re.findall(r'"([^"]+)"', fs.group(1)) if fs else None
                        entry["field_toggles"] = re.findall(r'"([^"]+)"', ft.group(1)) if ft else None
        report["ops_found"] = len(ops)
        wanted = {}
        for name in WANTED_OPS:
            e = ops.get(name)
            if not e:
                wanted[name] = None
                continue
            wanted[name] = {
                "ids": e["ids"],
                "patterns": sorted(e["patterns"]),
                "sources": sorted(e["sources"])[:5],
                "feature_switches": e.get("feature_switches"),
                "field_toggles": e.get("field_toggles"),
            }
            if e.get("feature_switches"):
                wanted[name]["features_resolved"] = {k: features.get(k, False) for k in e["feature_switches"]}
                wanted[name]["features_missing_in_config"] = [k for k in e["feature_switches"] if k not in features]
        report["wanted_ops"] = wanted
        save_json(OUT_DIR / "discovery.json", report, acc)
        save_json(OUT_DIR / "feature_config.json", features, acc)
        save_json(
            OUT_DIR / "all_ops.json",
            {k: {"ids": v["ids"], "patterns": sorted(v["patterns"])} for k, v in sorted(ops.items())},
            acc,
        )
        for name in WANTED_OPS:
            if ops.get(name, {}).get("snippet"):
                (OUT_DIR / f"snippet_{name}.js").write_text(ops[name]["snippet"])
        print(json.dumps({k: v for k, v in report.items() if k != "wanted_ops"}, indent=2))
        for name, info in wanted.items():
            if info is None:
                print(f"{name}: NOT FOUND")
            else:
                print(f"{name}: ids={info['ids']} patterns={info['patterns']} "
                      f"switches={len(info['feature_switches'] or [])} toggles={info['field_toggles']} "
                      f"missing_in_config={info.get('features_missing_in_config')}")
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
