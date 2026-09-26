"""Phase 0.4 + 0.5 + 0.6: TID layers, limit headers, GET vs POST, query length, fixtures."""

from __future__ import annotations

import asyncio
import json
import random
import string
from typing import Any

from curl_cffi.requests import AsyncSession

from common import BEARER_ALT, FIXTURES_DIR, IMPERSONATE, OUT_DIR, Account, Prober, make_session, save_json
from xclid import XClIdGen

PAIR_URL = "https://raw.githubusercontent.com/fa0311/x-client-transaction-id-pair-dict/refs/heads/main/pair.json"
PROFILE = "XDevelopers"
KEYWORD = "bitcoin"
TAIL_USER = "elonmusk"
REAL_HANDLES = [
    "elonmusk", "VitalikButerin", "cz_binance", "saylor", "brian_armstrong", "APompliano",
    "CoinDesk", "Cointelegraph", "WatcherGuru", "whale_alert", "tier10k", "DocumentingBTC",
    "BitcoinMagazine", "binance", "coinbase", "krakenfx", "solana", "ethereum", "Bybit_Official", "okx",
]
QUERY_LENGTHS = [500, 1000, 1500, 2500, 4000]


def iter_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_dicts(v)


def timeline_entries(body: Any) -> list[dict]:
    entries: list[dict] = []
    for d in iter_dicts(body):
        if isinstance(d.get("entries"), list):
            entries.extend(e for e in d["entries"] if isinstance(e, dict))
    return entries


def tweet_authors(body: Any) -> list[str]:
    authors = []
    for e in timeline_entries(body):
        if not str(e.get("entryId", "")).startswith("tweet-"):
            continue
        for d in iter_dicts(e):
            ur = d.get("user_results")
            if isinstance(ur, dict):
                names = [x.get("screen_name") for x in iter_dicts(ur) if isinstance(x.get("screen_name"), str)]
                if names:
                    authors.append(names[0])
                    break
    return authors


def bottom_cursor(body: Any) -> str | None:
    for d in iter_dicts(body):
        if d.get("cursorType") == "Bottom" and isinstance(d.get("value"), str):
            return d["value"]
    return None


def user_rest_id(body: Any) -> str | None:
    try:
        return body["data"]["user"]["result"]["rest_id"]
    except (KeyError, TypeError):
        return None


def filler_query(total_len: int) -> str:
    tail = f" OR from:{TAIL_USER}"
    terms: list[str] = []
    while len(" OR ".join(terms)) + len(tail) + 30 < total_len:
        terms.append("from:zq" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8)))
    return "(" + " OR ".join(terms) + tail + ")"


async def main() -> None:
    acc = Account.load()
    disc = json.loads((OUT_DIR / "discovery.json").read_text())["wanted_ops"]
    ops = {name: (next(iter(info["ids"])), info.get("features_resolved") or {}) for name, info in disc.items() if info}
    session = make_session(acc)
    prober = Prober(acc, session)
    findings: dict[str, Any] = {"ops": {k: v[0] for k, v in ops.items()}}
    fixtures: dict[str, Any] = {}

    def features(name: str) -> dict:
        return {k: bool(v) if isinstance(v, bool) else v for k, v in ops[name][1].items()}

    user_toggles = {"withAuxiliaryUserLabels": False}
    search_toggles = {"withArticleRichContentState": True}

    def search_vars(q: str, product: str = "Latest") -> dict:
        return {"rawQuery": q, "count": 20, "querySource": "typed_query", "product": product}

    try:
        # 0.4 TID layers
        home = (OUT_DIR / "home.html").read_text()
        try:
            gen = await XClIdGen.from_page(session, home)
            findings["tid_generator"] = {"ok": True, "source": gen.source}
        except Exception as e:
            gen = None
            findings["tid_generator"] = {"ok": False, "error": repr(e)}
        async with AsyncSession(impersonate=IMPERSONATE, timeout=30) as plain:
            rep = await plain.get(PAIR_URL)
            pairs = rep.json() if rep.status_code == 200 else []
        pair_gen = XClIdGen.from_pair(**{"verification": pairs[0]["verification"], "animation_key": pairs[0]["animationKey"]}) if pairs else None
        findings["pair_dict"] = {"status": rep.status_code, "pairs": len(pairs)}
        print("TID:", findings["tid_generator"], findings["pair_dict"])

        ubsn_id = ops["UserByScreenName"][0]
        ubsn_vars = {"screen_name": PROFILE, "withSafetyModeUserFields": True}
        variants = [
            ("generator", gen, None),
            ("no_tid", None, None),
            ("pair_dict", pair_gen, None),
            ("no_tid_alt_bearer", None, BEARER_ALT),
        ]
        tid_matrix: dict[str, dict] = {}
        profile_body = None
        for label, g, bearer in variants:
            kwargs = {"bearer": bearer} if bearer else {}
            r = await prober.gql(f"UserByScreenName[{label}]", ubsn_id, "UserByScreenName", ubsn_vars,
                                 features("UserByScreenName"), user_toggles, tid_gen=g, **kwargs)
            if r:
                tid_matrix[f"UserByScreenName/{label}"] = r.summary()
                if r.status == 200 and user_rest_id(r.body) and profile_body is None:
                    profile_body = r.body
        st_id = ops["SearchTimeline"][0]
        search_body = None
        for label, g, bearer in variants:
            kwargs = {"bearer": bearer} if bearer else {}
            r = await prober.gql(f"SearchTimeline[{label}]", st_id, "SearchTimeline", search_vars(KEYWORD),
                                 features("SearchTimeline"), search_toggles, tid_gen=g, **kwargs)
            if r:
                tid_matrix[f"SearchTimeline/{label}"] = r.summary()
                if r.status == 200 and timeline_entries(r.body) and search_body is None:
                    search_body = r.body
        findings["tid_matrix"] = tid_matrix
        if profile_body:
            fixtures["user_by_screen_name"] = profile_body
        if search_body:
            fixtures["search_latest"] = search_body

        best = gen or pair_gen

        # 0.5 GET vs POST
        r = await prober.gql("SearchTimeline[POST]", st_id, "SearchTimeline", search_vars(KEYWORD),
                             features("SearchTimeline"), search_toggles, method="POST", tid_gen=best)
        findings["search_post"] = r.summary() if r else None

        # Other products and operations
        r = await prober.gql("SearchTimeline[Top]", st_id, "SearchTimeline", search_vars(KEYWORD, "Top"),
                             features("SearchTimeline"), search_toggles, tid_gen=best)
        if r and r.status == 200:
            fixtures["search_top"] = r.body
        r = await prober.gql("SearchTimeline[People]", st_id, "SearchTimeline", search_vars(KEYWORD, "People"),
                             features("SearchTimeline"), search_toggles, tid_gen=best)
        if r and r.status == 200:
            fixtures["search_people"] = r.body
        uid = user_rest_id(profile_body) if profile_body else None
        if uid and "UserTweets" in ops:
            ut_vars = {"userId": uid, "count": 20, "includePromotedContent": False,
                       "withQuickPromoteEligibilityTweetFields": True, "withVoice": True}
            r = await prober.gql("UserTweets[p1]", ops["UserTweets"][0], "UserTweets", ut_vars,
                                 features("UserTweets"), search_toggles, tid_gen=best)
            if r and r.status == 200:
                fixtures["user_tweets_p1"] = r.body
                cur = bottom_cursor(r.body)
                if cur:
                    r2 = await prober.gql("UserTweets[p2]", ops["UserTweets"][0], "UserTweets",
                                          {**ut_vars, "cursor": cur}, features("UserTweets"), search_toggles, tid_gen=best)
                    if r2 and r2.status == 200:
                        fixtures["user_tweets_p2"] = r2.body

        # 0.5 batched from: query with real handles
        q_real = "(" + " OR ".join(f"from:{h}" for h in REAL_HANDLES) + ")"
        r = await prober.gql("SearchTimeline[batch20]", st_id, "SearchTimeline", search_vars(q_real),
                             features("SearchTimeline"), search_toggles, tid_gen=best)
        if r:
            authors = tweet_authors(r.body) if r.body else []
            findings["batch_real_handles"] = {**r.summary(), "query_len": len(q_real), "tweets": len(authors),
                                              "distinct_authors": sorted(set(authors))}
            if r.status == 200:
                fixtures["search_batch_from"] = r.body

        # 0.5 query length: fillers that match nobody + a busy tail user
        qlen: list[dict] = []
        for n in QUERY_LENGTHS:
            q = filler_query(n)
            r = await prober.gql(f"SearchTimeline[len{len(q)}]", st_id, "SearchTimeline", search_vars(q),
                                 features("SearchTimeline"), search_toggles, tid_gen=best)
            if not r:
                break
            authors = tweet_authors(r.body) if r.body else []
            qlen.append({**r.summary(), "query_len": len(q), "terms": q.count("from:"), "tweets": len(authors),
                         "tail_user_found": TAIL_USER in authors})
            if r.status != 200 or TAIL_USER not in authors:
                break
        findings["query_length"] = qlen
        r = await prober.gql("SearchTimeline[empty]", st_id, "SearchTimeline", search_vars(filler_query(120)[:-len(f" OR from:{TAIL_USER})")] + ")"),
                             features("SearchTimeline"), search_toggles, tid_gen=best)
        if r and r.status == 200:
            fixtures["search_empty"] = r.body

        findings["rate_limits"] = {
            res.op: {"limit": res.limit, "remaining": res.remaining} for res in prober.results if res.limit is not None
        }
        findings["calls"] = [res.summary() for res in prober.results]
        save_json(OUT_DIR / "calls.json", findings, acc)
        for name, body in fixtures.items():
            save_json(FIXTURES_DIR / f"{name}.json", body, acc)
        print("fixtures:", sorted(fixtures))
        print("rate_limits:", json.dumps(findings["rate_limits"]))
        print("batch:", json.dumps(findings.get("batch_real_handles", {}).get("distinct_authors")))
        print("query_length:", json.dumps([{k: x[k] for k in ("query_len", "terms", "status", "tweets", "tail_user_found")} for x in qlen]))
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
