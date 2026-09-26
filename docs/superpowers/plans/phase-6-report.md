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

## Pending (needs the owner's accounts)

- 24 h soak with 6–10 accounts: `uv run xscout serve` (or pm2), then
  `uv run xscout soak --minutes 1440 --rpm 20`. From the report: per-account
  spread, 503 rate, cache ratio, p50/p95, canary timeline, any lock; then
  calibrate reserve, gap, P0/P1 shares, TTLs and decide whether the overflow
  buckets stay on. Also observe a live KOL post reaching the feed
  (phase 5 open item) and whether several accounts on one IP hit a per-IP
  limit (spec §2 open item).
