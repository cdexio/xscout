"""Phase 0.5 follow-up: are POST and alternative-bearer SearchTimeline separate limit buckets, with full data?"""

from __future__ import annotations

import asyncio
import json

from calls import KEYWORD, timeline_entries, tweet_authors
from common import BEARER_ALT, BEARER_MAIN, FIXTURES_DIR, OUT_DIR, Account, Prober, make_session, save_json
from xclid import XClIdGen

VARIANTS = [
    # (label, method, bearer, use_tid)
    ("GET/main/tid", "GET", BEARER_MAIN, True),
    ("POST/main/tid", "POST", BEARER_MAIN, True),
    ("POST/main/tid#2", "POST", BEARER_MAIN, True),
    ("GET/main/tid#2", "GET", BEARER_MAIN, True),
    ("GET/alt/no_tid", "GET", BEARER_ALT, False),
    ("POST/alt/no_tid", "POST", BEARER_ALT, False),
    ("POST/alt/tid", "POST", BEARER_ALT, True),
    ("POST/main/no_tid", "POST", BEARER_MAIN, False),
]


async def main() -> None:
    acc = Account.load()
    disc = json.loads((OUT_DIR / "discovery.json").read_text())["wanted_ops"]["SearchTimeline"]
    op_id = next(iter(disc["ids"]))
    features = disc["features_resolved"]
    toggles = {"withArticleRichContentState": True}
    variables = {"rawQuery": KEYWORD, "count": 20, "querySource": "typed_query", "product": "Latest"}
    session = make_session(acc)
    prober = Prober(acc, session)
    rows = []
    try:
        gen = await XClIdGen.from_page(session, (OUT_DIR / "home.html").read_text())
        for label, method, bearer, use_tid in VARIANTS:
            # Prober guards per operation name; key the guard per bucket so one bucket cannot block another.
            op_key = f"SearchTimeline::{method}::{'alt' if bearer == BEARER_ALT else 'main'}"
            prober.remaining.setdefault(op_key, 999)
            if prober.remaining[op_key] <= 10:
                print(f"  skip {label}")
                continue
            r = await prober.gql(label, op_id, "SearchTimeline", variables, features, toggles,
                                 method=method, tid_gen=gen if use_tid else None, bearer=bearer)
            if r is None:
                continue
            if r.remaining is not None:
                prober.remaining[op_key] = r.remaining
            prober.remaining.pop("SearchTimeline", None)
            authors = tweet_authors(r.body) if r.body else []
            rows.append({**r.summary(), "entries": len(timeline_entries(r.body)) if r.body else 0,
                         "tweets": len(authors)})
            if label == "POST/main/tid" and r.status == 200:
                save_json(FIXTURES_DIR / "search_latest_post.json", r.body, acc)
        save_json(OUT_DIR / "buckets.json", rows, acc)
        for row in rows:
            print(f"{row['label']:<18} status={row['status']} rate={row['rate']} reset_in={row['reset_in_sec']} "
                  f"tweets={row['tweets']} errors={row['error_codes']}")
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
