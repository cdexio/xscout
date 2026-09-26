# Phase 3 report — Pool + gateway (2026-09-26)

## Built

- `pool/pool.py`: in-memory quota state per (account, operation, bucket)
  with window rollover at `reset_at`, optimistic decrement on lease,
  header-corrected on release. Selection: active, not cooling, gap elapsed,
  not in flight; headroom = remaining − max(5, 10% of limit); primary
  buckets before overflow (overflow switchable globally and per account);
  highest headroom first, then least recently used. `NoLease` carries the
  earliest time an account frees up. Strike counter for 429-with-quota.
- `pool/budget.py`: rolling 15-minute window; P0 share, P1 share plus P0's
  unused share, P2 only what leaves P0's unused share free; capacity from
  the pool's live headroom; per-consumer usage.
- `gateway/classify.py`: response → verdict table (spec §5 plus phase 0:
  214 inside HTTP 200 is a caller error; 429 with quota left is abuse,
  429 at zero is the normal window end; 326 locked; 32/89/239/353 expired;
  37/63/64 suspended; 404 and unhealed 336 trigger registry refresh; HTML
  is Cloudflare; 5xx/130/131/-1 transient).
- `cache/cache.py`: LRU with TTL per operation, `max_age_sec`, stale lookup,
  in-flight coalescing.
- `gateway/gateway.py`: cache → coalescing → budget → lease (P0 waits up
  to `max_wait_sec_p0`) → call → classify → release → retry on another
  account (max 3); partial multi-page results are kept; stale cache is
  served to P0 when unavailable; three strikes in 24 h lock an account.
- `gateway/runtime.py`: wiring and background loops (state flush, account
  sync from the DB every 60 s so CLI-added accounts join without restart,
  registry refresh every 6 h and on 404/336 at most every 10 min); persists
  status changes, rotated `ct0`, samples, request log, per-account last use.
- Stores: `rate_state.py`, `request_log.py`, account runtime updates.
- CLI: `xscout x burst` (concurrent searches through the gateway, shows the
  spread) and `xscout pool` (stored quota state).

## Verified

- 116 tests pass (unit + DB): classification table, even spread over
  accounts (±1 over 100 leases and over 30 concurrent gateway requests),
  reserve never used, overflow last and switchable, rollover, cooldown
  scopes, strikes → lock, budget shares and borrowing, cache TTL/stale/LRU,
  coalescing, multi-page with partial result, stale serving, 404 refresh.
- Live (one account): 5 distinct queries × 2 concurrent → 3 X requests,
  5 coalesced, all POST/main; quota state (184/187 with reset), request log
  and last-use persisted and reloaded by `xscout pool`.

## Finding: single-account throughput

With one account, the per-account gap (2–4 s) caps throughput at about
20 requests per minute. In the live burst, 2 of 5 distinct concurrent
queries could not start within the P0 wait of 8 s (zetryn's timeout) and got
`503` with `retry_after 4 s`, as designed. Throughput scales with accounts:
6–10 accounts give roughly 2–5 requests per second.

Options:

1. **Add accounts (recommended, already planned).** No code change.
2. Shorten the gap (e.g. 1–2 s). The 2–4 s value is a cautious guess, not
   measured; browsers fire several GraphQL calls per page load. Tune from
   the phase 6 soak data instead of guessing now.
3. Longer P0 wait. Only useful if consumer timeouts grow too.
