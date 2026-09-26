# Phase 4 — HTTP API (on-demand)

Goal: the bots can query X through xscout over loopback HTTP with a stable,
documented contract.

## Tasks

### 4.1 App and middleware
- FastAPI app bound to `127.0.0.1`; required `X-Consumer` header;
  response envelope and error format per spec §7; `Retry-After` on 503.
- Startup: DB check, registry refresh, TID warm-up; shutdown closes
  sessions cleanly.

### 4.2 Endpoints
- `GET /v1/search/tweets`, `GET /v1/search/users`,
  `GET /v1/users/{username}`, `GET /v1/users/{username}/tweets`, all P0
  through the gateway; `limit` → page count with a configured cap;
  cursor pass-through.
- Upsert returned tweets and users into the store.
- `GET /v1/accounts`, `GET /v1/budget`, `GET /health` (health combines
  pool, budget and xweb layer status; canary joins in phase 6).

### 4.3 Operations
- pm2 entry for `xscout`, JSON logs, a runbook section in the project
  README (start, stop, add account, read health).
- OpenAPI schema exported to `docs/openapi.json` for consumers.

## Verification
- API tests with FastAPI's test client and a fake gateway: validation,
  envelope, 404, 503 with retry-after, stale responses, missing
  `X-Consumer`.
- Live: curl each endpoint; two concurrent identical searches produce one
  X request.
