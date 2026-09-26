# Phase 4 report — HTTP API (2026-09-26)

## Built

- `api/service.py`: use cases behind the API — `limit` → pages (20 per page,
  capped by `api.max_pages`), dedup across pages, username → id with a
  one-hour profile reuse, envelopes, background archiving of fresh results
  (`store/tweets.py`, idempotent upsert that updates engagement counts).
- `api/app.py`: FastAPI app; required `X-Consumer`; envelope
  `data / includes / next_cursor / meta`; errors
  `error {code, message, retry_after_sec}` with `Retry-After` on 503;
  validation errors mapped to 400.
- `api/main.py`: runtime-backed app; `xscout serve` (uvicorn, JSON logs,
  bound to `XSCOUT_HOST`, default loopback), `xscout openapi` →
  `docs/openapi.json`; `ecosystem.config.cjs` for pm2; README runbook and
  consumer guide.
- Status reports on the runtime: `/health` (ok/degraded/down with problems,
  registry sources, TID layers, capacity, cache and gateway counters),
  `/v1/accounts` (pool snapshot, no secrets), `/v1/budget` (capacity left,
  usage per priority and per consumer).

## Verified

- 126 tests pass; ruff check and format clean. Test client moved to
  `httpx2` (Starlette 1.7 deprecates `httpx` for its test client).
- Live on `127.0.0.1:8790` (listener confirmed loopback only):
  - `$SOL` search, limit 30 → 2 X requests, 30 tweets; the same query from
    another consumer → served from cache, no X request;
  - user search, profile, user tweets (pinned first, 13 originals in
    `includes`); the profile lookup inside `/tweets` reused the cache;
  - 404 for an unknown user, 400 without `X-Consumer`, 400 for a query over
    512 characters;
  - `/v1/budget` shows usage per consumer; request log complete;
  - graceful shutdown on SIGTERM.

## Change to the spec

- `include_replies` on `/v1/users/{username}/tweets` is deferred: it needs
  the `UserTweetsAndReplies` operation, which has not been verified live.
