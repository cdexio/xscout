# Phase 6 report — Canary + resilience (2026-09-26, code done; 24 h soak pending)

## Built

- `canary/canary.py`: profile of `XDevelopers`, a `bitcoin` Latest search and
  that profile's tweets on P2 with `max_age_sec=0`. States per operation:
  ok / degraded (a key field null in ≥ 20% or failed entries) / broken (≥ 80%
  nulls, too few items, 404, unhealed 336, bad request) / skipped (no quota;
  keeps the last real verdict). A new `broken` calls `heal` (forced registry
  rediscovery bypassing the throttle, TID rebuild on every account) and
  re-probes once. State changes are logged and kept in `kv_state`
  (`canary:history`, last 100). Runs 30 s after start, then every 10 min;
  `/health` turns `degraded` with the reason.
- Null-rate spike samples: the account client stores a raw response when a
  key field is null in ≥ 50% of a page with ≥ 5 items (once per operation per
  10 min), in addition to parse failures.
- `canary/upstream.py` + `xscout upstream`: GitHub commits since the stored
  cursor for the verified paths of twscrape, XClientTransaction, Nitter and
  x-client-transaction-id; merged per commit; the cursor advances only when
  every request succeeded.
- `store/maintenance.py`: `kv_state` (migration `0003`), retention (tweets and
  feed 90 days, samples 7, request log 30, expired cache rows), sample
  reader. Retention runs 5 min after start, then every 6 h;
  `xscout retention` runs it now.
- `xscout samples list|promote` (promote writes a redacted, pretty fixture).
- `xscout soak`: bot-shaped load (zetryn 50% memecoin cashtags max_age 60,
  cdexio 30% futures max_age 300, stocks 20% max_age 600, 15% profile
  lookups), watchlist of up to 30 KOLs and 3 queries sized to a share of the
  P1 capacity, `/health` snapshots each minute, JSON report every 5 minutes
  in `logs/`, watch items removed at the end.

## Verified

- 153 tests pass; `alembic check` clean after `0003`. Canary tests simulate
  a stale queryId (404), a rejected TID (404) and a schema change (authors
  gone): each goes `broken`, heals exactly once and re-probes; recovery
  after heal returns `ok`; quota shortage is `skipped`, not broken.
- Live: `xscout canary` → all three `ok`; `xscout retention` ran;
  `xscout upstream` returned 7 relevant commits in 30 days.
- **Upstream watch paid off on its first run.** Commit 2f1f95b in
  `Lqm1/x-client-transaction-id` (2026-09-25): logged-out `/home` now
  redirects to the x-web build, which no longer exposes the TID inputs;
  they moved to `x.com/i/jf/`. Our logged-in `/home` still serves
  responsive-web, but both pages were verified live to build a TID, and
  `/i/jf/` is now the fallback page before the pair-dict layer.
- First upstream run failed with 403 for every path: GitHub rejects requests
  without a User-Agent. Fixed (verified: 403 without, 200 with).
- Soak smoke run (4 min, 8 rpm, one account): 43 on-demand requests,
  41 X requests all ok (34 POST/main, 7 GET/main), account active, canary ok
  throughout, 33 watch items created and removed. One 503 and 5 postponed
  watch polls, p95 up to 8 s: the single-account gap limit from phase 3.

## 24 h soak (2026-09-29 12:39 → 2026-09-30 12:39 UTC, production VPS)

Setup: 6 accounts, SG VPS (one datacenter IP), `xscout soak --minutes 1440 --rpm 20`, watchlist of
29 KOLs + 3 queries (45 requests per 15 min projected). Accounts were moved from the laptop
encrypted; the VPS IP passed discovery, TID and search with one account before all were enabled.
A 32-minute laptop run before the move gave the same picture (568 requests, 0×503, 53–55 X
requests per account).

| Metric | Result |
|---|---|
| On-demand requests (3 consumers) | 28,709, **all 200**, 0×503, 0 stale |
| Cache ratio | zetryn 46% (max_age 60 s), cdexio 84% (300 s), stocks 86% (600 s); 68% of on-demand calls never reached X |
| Latency seen by bots (uncached) | p50 0.76–0.90 s, p95 1.34–1.52 s |
| X requests | 14,924: P0 9,089, P1 watchlist 4,214, P2 canary 396 |
| Outcomes | 14,923 ok, 1 network error; **no 429, no code 88, no Cloudflare, no 404/336** |
| Spread over accounts | 2,450–2,506 per account (±1.1%) |
| Buckets used | POST/main 13,184, GET/main 1,740; overflow (alt bearer) **never needed** |
| Peak per account per 15 min | SearchTimeline 27 of 187 (14%), UserByScreenName 6 of 150, UserTweets 1 of 50 |
| Peak from the one IP per 15 min | 175 (avg 154) |
| Watchlist | 4,421 polls, 18,273 feed entries, 0 errors |
| Canary | `ok` for 24 h (only the initial unknown → ok transitions); 0 response samples |
| Accounts | all 6 `active` throughout; no status change |
| Resources | service ~100 MB RSS; DB 53 MB (tweets 34 MB for 41,231 tweets/day ≈ 3 GB at 90-day retention; 97 GB free) |

Answers to the open items:

- **Live KOL post to feed** (phase 5): confirmed; e.g. `@whale_alert` in 9–16 s,
  `@notthreadguy` 37 s during the laptop run; 18,273 watch entries on the VPS.
- **Per-IP limit** (spec §2): none observed up to 175 X requests per 15 min (≈ 14,900 per day)
  from one IP with 6 accounts. A limit above that volume stays possible.
- **Overflow buckets**: never triggered; at this load primary buckets use ≤ 14% of their quota.

## Calibration

The load used a small part of the capacity, so the data supports keeping the safety margins
rather than loosening them:

- Gap 2–4 s, reserve `max(5, 10%)`, cache TTLs: **keep** (no strain, no 503, no 429).
- P0/P1 shares: P0 made 2.2× the requests of P1, and P0 cannot borrow unused P1. Proposal:
  **P0 0.6 / P1 0.4** (P1 still ~1,000 requests per 15 min, 23× the soak watchlist).
- Overflow buckets: **keep enabled** as a safety valve; they only activate when primary buckets
  are exhausted, which did not happen.
- Capacity for the bots: 6 accounts give ~1,280 primary search requests per 15 min; zetryn's
  measured need was ~180 (its phase 6 report), so about 7× headroom before overflow.
