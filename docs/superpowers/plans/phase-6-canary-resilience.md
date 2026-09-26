# Phase 6 — Canary + resilience

Goal: xscout notices X changes by itself, keeps its data bounded, and
proves it survives a full day with the real account pool.

## Tasks

### 6.1 Canary
- Known-answer probes every 10 min on P2 budget (large-account profile,
  busy-keyword search, large-account tweets).
- Checks: status, TID accepted, queryId valid, null rate of key fields.
- Component states `ok` / `degraded` / `broken` with reason and sample
  reference, merged into `/health`; state changes logged as warnings.
- On `broken`: trigger registry refresh and TID rebuild automatically,
  then re-probe.

### 6.2 Samples and fixtures
- Response samples on parse failure or null-rate spikes; 7-day retention.
- CLI command to promote a sample to a sanitized test fixture.

### 6.3 Upstream watch
- CLI command listing new commits in twscrape, XClientTransaction, Nitter
  and `x-client-transaction-id` (npm) that touch TID, queryId/features or
  parser paths since the last check; last-checked state stored in DB.

### 6.4 Retention jobs
- Tweets 90 days, samples 7 days, request log 30 days, expired cache
  entries; all configurable.

### 6.5 Soak
- 24 h run with 6–10 accounts at a realistic mix: on-demand calls from a
  load script shaped like the three bots, plus a watchlist near the P1
  share.
- Calibrate reserve, gap, shares and TTLs from the collected headers.

## Verification
- Canary tests: simulated 404, TID rejection and schema change each move
  the right component to `broken` and trigger self-heal.
- Soak report: no locked or suspended account, requests per account
  within a narrow band, 503 rate, cache hit rate, p50/p95 latency,
  canary state timeline.

## Owner inputs
- 6–10 dedicated accounts added through the CLI before the soak.
