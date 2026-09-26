# xscout — design spec

- Date: 2026-09-26
- Status: approved in brainstorming, pending written-spec review
- Location: `zesch/x/`
- Plans: one file per phase under `docs/superpowers/plans/`

## 1. Purpose

xscout is a self-hosted X (Twitter) scraping service that authenticates with
browser cookies (`auth_token` + `ct0`) and calls X's internal web GraphQL
API. It serves three trading bots on the same machine:

- **cdexio** — futures bot (Python), no X integration today.
- **zetryn** — memecoin bot (TypeScript), currently uses a twscrape-based
  Python sidecar on `127.0.0.1:8765`.
- **stock bot** — not built yet.

The goal is to stop hitting the limits that twikit and the current sidecar
hit, by owning the parts that decide limit behaviour: fair account rotation,
stopping before quota runs out, one shared budget and cache for all bots,
batched watchlists, and browser-like requests.

### Non-goals

- Bypassing X's server-side limits. They are per account and per operation;
  capacity grows only with more accounts.
- Write actions (post, like, follow, DM). Read-only.
- Programmatic login. Accounts are added as cookies exported from a real
  browser.
- Domain scoring (mention scores, KOL signals). Each bot keeps its own logic;
  xscout returns clean, normalized data.
- Access from other machines. Loopback only.

## 2. Verified facts this design rests on

Researched 2026-09-26 against source code, commits and issues of twikit,
twscrape, XClientTransaction, Nitter, the-convocation/twitter-scraper and
imperatrona/twitter-scraper, plus the zetryn phase-6 live soak. `[V]` =
verified, `[U]` = uncertain and must be settled in phase 0.

- `[V]` twikit is effectively abandoned: last release 2.3.3 (2025-02), its
  transaction-id module has been broken since 2026-03-18 (issue #408), its
  SearchTimeline queryId returns 404, and fix PRs go unmerged. It uses plain
  httpx (no TLS impersonation) and hits Cloudflare 403s (#388, #396).
- `[V]` twscrape is active (0.20.1, 2026-08-25; commits to 2026-09-23), MIT
  licensed. Its pool picks the first unlocked account ordered by username
  and rotates only after a lock. The zetryn soak showed one account serving
  every request until a lock, and a 3-account budget exhausted in 3 minutes.
- `[V]` Limits are reported per response in `x-rate-limit-limit`,
  `x-rate-limit-remaining`, `x-rate-limit-reset` (unix time), in 15-minute
  windows, scoped per account and per operation. Observed/documented values:
  SearchTimeline ~50, UserTweets ~50, UserByScreenName ~95,
  TweetDetail ~150, UserByRestId ~500.
- `[U]` An additional per-IP limit may exist (twikit #381, twscrape #274).
- `[V]` A browser-like TLS/HTTP2 fingerprint matters most against Cloudflare
  blocks: twscrape #337 (httpx blocked, curl_cffi works; the maintainer
  wrote "httpx is too easily detectable"), twikit #388/#396.
- `[V]` Nitter keeps sessions from reaching zero (limited when
  `remaining <= 10`), benches a session one hour on error 88, and caps two
  concurrent requests per session.
- `[V]` `x-client-transaction-id` (TID) is derived from the x.com page
  (`twitter-site-verification` meta, `loading-x-anim-*` SVG frames, index
  pattern in an `ondemand.s` chunk). It breaks whenever X changes page or
  bundle layout: six fixes across XClientTransaction and twscrape between
  2025-10 and 2026-08. A missing or stale TID causes 404s on some operations.
- `[U]` Whether a missing TID also raises a hidden bot score.
- `[V]` GraphQL queryIds rotate often: SearchTimeline changed about 7 times
  between 2026-05 and 2026-08. They can be discovered from x.com JS bundles
  (`queryId:"…",operationName:"…"` and the newer
  `params:{id,name,operationKind}` pattern). The response schema also
  changes (2026-05: `legacy` became null, user fields moved to `core`,
  `avatar`, `location`, `verification`, …).
- `[U]` Whether SearchTimeline now requires POST (twikit PR #412,
  the-convocation 0.22.3) or still works with GET (twscrape, verified
  2026-05-20).
- `[V]` Precomputed TID key pairs are published hourly at
  `fa0311/x-client-transaction-id-pair-dict` (used by Nitter). Nitter also
  has a no-TID mode with an alternative bearer token (`disableTid`).
- `[V]` Suspension risk is higher with programmatic login than with cookies
  exported from a browser (anecdotal, twikit #359). Scraping is against X's
  terms; X sent Nitter a cease-and-desist in 2026-08. Only dedicated,
  replaceable accounts are used.
- `[U]` Maximum length of a search query (limits the size of batched
  `from:a OR from:b` watch queries).

### 2.1 Phase 0 live results (2026-09-26)

Measured with one account; details in `../plans/phase-0-report.md`.
These override the values above where they differ.

- `[V]` All dependencies work on Python 3.14.7.
- `[V]` Limits per 15 min: UserByScreenName 150, SearchTimeline 50 (GET),
  UserTweets 50.
- `[V]` **Limit buckets are per (account, operation, method, bearer).**
  SearchTimeline: GET+main 50, POST+main 187, GET+alt 50, POST+alt 187, all
  returning full pages. UserByScreenName with the alternative bearer has its
  own 150 bucket.
- `[V]` GET SearchTimeline without a TID returns 404; POST works without a
  TID; UserByScreenName works without a TID. Own generator and pair-dict
  TIDs are both accepted.
- `[V]` Discovery from the logged-in legacy build's `main.js` finds 105
  operations with their feature switches and field toggles. X accepts more
  than one queryId per operation at a time.
- `[V]` Search query max length is 512 characters; over it X answers
  HTTP 200 with error 214 and no entries.
- `[V]` User objects no longer have `legacy`; counts are in
  `relationship_counts` and `tweet_counts`. Tweets still have `legacy`.
- `[U]` Per-IP limit and hidden bot score from missing TID remain open
  (phase 6 soak).

## 3. Decisions

| Topic | Decision |
|---|---|
| Accounts | 6–10+ dedicated accounts, replaced when suspended |
| IP / proxy | No proxy at first; optional `proxy` field per account from day one (static residential/ISP recommended later; rotating proxies are not used) |
| Consumption | One HTTP service on `127.0.0.1`, shared by all bots |
| v1 operations | Search tweets, search users, user profile, user tweets, watchlist + feed. Tweet detail/replies later |
| Storage | PostgreSQL, a dedicated `xscout` database on the local server |
| Secrets | Cookies encrypted with Fernet, key in `.env` |
| Watchlist freshness | Per-item interval, priority-aware scheduler |
| Approach | Own all code (3b): fragile pieces are vendored from MIT sources, not imported; multi-source fallbacks, self-healing, canary and upstream watch |
| Language | Python 3.12+ |

## 4. Architecture

```
 cdexio ─┐
 zetryn ─┼─HTTP─► api ──► gateway ──► pool ──► transport ──► x.com
 stock  ─┘         │        │  ▲        │          │
                   │        ▼  │        ▼          ▼
               scheduler   cache    store (Postgres)   xweb (ops/features/tid/parser)
                (watchlist)                                ▲
                                       canary ─────────────┘
```

Stack: FastAPI + uvicorn (OpenAPI schema lets zetryn generate a typed
client), curl_cffi, SQLAlchemy 2 async + asyncpg + Alembic, `cryptography`
(Fernet), uv, pm2. Exact versions and Python-version compatibility
(the machine runs 3.14) are verified in phase 0.

### Components

| Component | Responsibility | Depends on |
|---|---|---|
| **api** | REST endpoints, validation, mapping to response models. Never talks to X. | gateway, store |
| **gateway** | The only path to X. Takes a job (operation, variables, priority, consumer), checks cache, coalesces identical in-flight jobs, leases an account, executes, records limit headers, classifies errors, retries on another account when allowed. | pool, transport, xweb, cache |
| **pool** | Chooses accounts, tracks limit state per (account, operation), account status and cooldowns, concurrency cap. | store |
| **transport** | One curl_cffi session per account with a fixed browser impersonation profile, optional proxy, header builder (bearer, csrf, TID, client language). | xweb (TID) |
| **xweb** | Everything X can break: operation registry (queryId, method, features), TID provider, parsers into our own models. | transport (to fetch pages/bundles) |
| **scheduler** | Runs watchlist items on their intervals, batches user watches, enforces the P1 budget share. | gateway, store |
| **cache** | Result cache keyed by operation + normalized variables, TTL per operation, `max_age_sec` override, stale serving. | store / memory |
| **store** | Postgres repositories and migrations. | — |
| **canary** | Periodic known-answer probes of the xweb layer; component health. | gateway |
| **cli** | Add account (hidden input), list, disable/enable, test one account, run upstream watch. | store, gateway |

Each component is testable alone behind a small interface; xweb is the only
component that knows X's wire format.

## 5. Account pool and anti-limit strategy

### Limit state

Stored per (account, operation, bucket), where bucket = method × bearer
(e.g. `POST/main`, `GET/alt`): `limit`, `remaining`, `reset_at`,
`last_used_at`, `cooling_until`. Updated from every response's headers.
Buckets never called by an account start from conservative defaults
(section 2.1 values) and are replaced by real headers after the first call.

Bucket policy per operation is configuration: an ordered list of allowed
buckets. Default for SearchTimeline: `POST/main` (primary), `GET/main`
(secondary), `POST/alt` and `GET/alt` as overflow only. The selector
picks, for the chosen account, the allowed bucket with the most headroom,
preferring earlier buckets on ties. Overflow buckets can be switched off
globally or per account.

### Account selection

1. Account status is `active`, it is not cooling for this operation, and it
   has no request in flight (max 1 concurrent per account, configurable).
2. `remaining` is above the reserve `max(5, 10% of limit)`. Accounts are
   retired for the window before quota reaches zero, so 429 should
   practically never occur.
3. Among eligible accounts: highest `remaining` first, then least recently
   used. Load spreads evenly across the pool.
4. A minimum gap per account between requests (default 2–4 s with random
   jitter).

Account statuses: `active`, `cooling` (per operation or global, time-bound),
`locked`, `expired`, `suspended`, `disabled` (manual).

### Error classification

| Signal | Action |
|---|---|
| `remaining` at or below reserve, or 0 | Cool the (account, operation) until `reset_at`; move on |
| 429 or code 88 while `remaining > 0` | Cool the account for 1 h on all operations; 3 times in 24 h → `locked` + warning |
| 326 | `locked` + warning (owner must solve the unlock in a browser) |
| 32, 89, 239 | `expired` + warning (new cookies needed) |
| 37, 63, 64 | `suspended`, never used again |
| 336 | Add the requested feature flags, retry once, persist the fix |
| 404 on a normally working operation | Refresh queryId and TID, retry on another account, report to canary |
| HTML / Cloudflare 403 | Cool the account 15 min, retry on another account, count occurrences |
| 353 | Treat as bad cookies (`ct0` missing/wrong) → `expired` |
| 214 (inside HTTP 200) | Input error (e.g. query over 512 chars) → `400` to the caller, account not penalized |
| Network error | Exponential backoff, account not penalized |
| Unknown error | Cool the (account, operation) 15 min, store a response sample |

### Budget and priority

- Capacity per operation = sum over usable accounts of (limit − reserve)
  within the current window.
- **P0 on-demand** (bot requests): reserved share 40% by default.
- **P1 watchlist** (scheduler): 60% by default; may borrow unused P0 share,
  never the reverse.
- **P2 backfill / canary**: leftover only.
- When P0 cannot be served: return cached data with `stale: true` and its
  age if any exists; otherwise `503` with `retry_after_sec`.

### Cache and coalescing

- Key: operation + normalized variables (query text trimmed, tab, page
  count, cursor).
- TTL per operation (config), overridable per request via `max_age_sec`.
- Identical jobs in flight share one X request.

All numbers above (reserve, gap, shares, TTLs, cooldowns) are configuration
and are calibrated from real header data during phases 0 and 6.

## 6. xweb layer

### Operation registry

- Discovery: fetch `x.com/home` with an account's cookies, collect
  `abs.twimg.com` script URLs (including `/x-web/` assets), follow
  `import()` references, extract queryId ↔ operationName pairs and default
  feature flags with both known patterns.
- Runs at startup, every 6 h, and immediately on repeated 404 or 336.
- Fallback chain: last successful discovery (stored in DB) → hardcoded
  constants. Every queryId change is recorded with a timestamp.
- Method per operation (GET or POST) is configurable. If the active method
  returns 404 right after a queryId refresh, try the other method and keep
  the one that works.

### TID provider (layered)

1. Own generator (vendored from XClientTransaction / twscrape, MIT, with
   attribution), one instance per account, built from pages fetched through
   that account's session. Cached; rebuilt every few hours or on failure.
2. Precomputed key pairs from `fa0311/x-client-transaction-id-pair-dict`.
3. No-TID mode with the alternative bearer token; health reports it as
   `degraded`.

### Parsers and models

- X responses are normalized into our own stable models: `Tweet`, `User`,
  `Page` (items + cursor). These models are the API contract for the bots.
- Tolerant parsing: each field is looked up in several known locations (old
  and new schemas); a missing field becomes `null` instead of raising.
  Entries that fail to parse are skipped and counted.
- Raw response samples are stored on parse failure or when the null rate of
  key fields spikes. Retention 7 days. Samples become test fixtures.

`Tweet` fields (v1): id, url, text, created_at, lang, author (User summary),
reply/retweet/quote/like/view/bookmark counts, is_reply, is_retweet,
is_quote, quoted/retweeted tweet id, conversation id, hashtags, cashtags,
mentions, urls, media (type + url).

`User` fields (v1): id, username, display name, bio, created_at,
followers, following, tweet count, verified (type), protected, location,
avatar url.

### Canary

- Every 10 min, on P2 budget: profile of a large account, a search for a
  busy keyword, tweets of a large account.
- Checks per operation: HTTP status, TID accepted, queryId valid, null rate
  of key fields (id, text, created_at, author, followers).
- Result in `/health` per component: `ok` / `degraded` / `broken`, with the
  reason and a sample reference. State changes are logged as warnings.
  Notifications (e.g. Telegram) are out of scope for v1.

### Upstream watch

A CLI command (manual or daily) that lists new commits in twscrape,
XClientTransaction, Nitter and `x-client-transaction-id` (npm) touching TID,
queryId/features or parser files since the last check. Early warning only;
nothing is applied automatically.

## 7. HTTP API (v1)

- Bind `127.0.0.1`. Every request carries `X-Consumer: <bot name>`, used for
  per-bot accounting and cache stats, not security.
- Response envelope: `data`, `next_cursor`, `meta` (`cached`, `stale`,
  `age_sec`, `fetched_at`, `pages_fetched`).
- Errors: `400` invalid input, `404` user not found, `409` watchlist over
  capacity, `503` `{reason, retry_after_sec}` with a `Retry-After` header.

| Endpoint | Purpose |
|---|---|
| `GET /v1/search/tweets?q=&tab=latest\|top&limit=&cursor=&max_age_sec=` | Tweet search with full X operators |
| `GET /v1/search/users?q=&limit=` | User search (People tab) |
| `GET /v1/users/{username}` | User profile |
| `GET /v1/users/{username}/tweets?limit=&include_replies=` | Latest tweets of a user |
| `POST/GET/PATCH/DELETE /v1/watchlist` | Manage watch items |
| `GET /v1/feed?tags=&since=&limit=&wait_sec=` | New watchlist tweets since a sequence number; long-poll up to 30 s |
| `GET /v1/budget` | Capacity, projected watchlist load, usage per bot |
| `GET /v1/accounts` | Account status, cooldowns, last error (never cookies) |
| `GET /health` | Component health, usable accounts, budget |

`limit` is converted to a page count (~20 items per page) and capped by
configuration.

## 8. Watchlist and feed

- Item: `kind` (`user` | `query`), `value`, `interval_sec`, `tags`,
  `enabled`.
- User watches with the same interval class are batched into
  `from:a OR from:b …` Latest searches of at most 500 characters
  (X's limit is 512, measured in phase 0; ≈ 25–30 handles). Each poll pages
  until it reaches the newest tweet id already seen for that batch (page
  cap in config), because busy accounts crowd quieter ones out of a single
  page. Fallback: UserTweets per user.
- Query watches: Latest search, deduplicated by already-seen tweet ids.
- Two bots watching the same value share one item: shortest interval wins,
  tags are merged.
- Capacity control: creating or changing an item computes projected
  requests per 15 min; over the P1 share → `409` with needed vs available.
- Feed: every new tweet found by the scheduler gets a monotonic sequence
  number linked to the items (and tags) that found it. Bots keep their last
  `since`. `wait_sec` long-polls the local DB only.

## 9. Storage

PostgreSQL database `xscout` (tests use `xscout_test`, guarded against
running on the main database). Main tables: accounts (encrypted cookies,
proxy, impersonation profile, status), rate_limit_state, operations
(queryId history, method, features), tweets, users, watch_items,
feed_entries, cache_entries, response_samples, request_log (compact,
per consumer and operation). Retention: tweets 90 days (configurable),
samples 7 days, request log 30 days.

## 10. Security

- Cookies encrypted with Fernet; key `XSCOUT_SECRET_KEY` in `.env`
  (git-ignored). Losing the key means re-adding accounts.
- CLI reads cookies with hidden input; they never appear in shell history,
  logs, or API responses.
- Logger redacts `auth_token`, `ct0`, `authorization`, `x-csrf-token`.
- Service binds to loopback only.

## 11. Testing

- Default run, no network: parsers against sanitized real fixtures, pool
  and budget with a fake clock, error classification, gateway with a fake
  transport, API with FastAPI's test client.
- DB integration tests on `xscout_test`.
- Live tests only with `pytest -m live` and a real account.
- Canary samples can be promoted to fixtures (regression tests for X schema
  changes).

## 12. Operations

pm2 process `xscout`, Alembic migrations, JSON structured logs,
configuration from env + YAML (all tunables in section 5).

## 13. Phases

Each phase has its own plan in `docs/superpowers/plans/` and is
independently deliverable and verifiable.

| Phase | Scope | Done when |
|---|---|---|
| 0 — Live verification | With one account: curl_cffi on our Python, queryId discovery, TID generation, real limit headers per operation, max search query length, GET vs POST for SearchTimeline; record fixtures | Every `[U]` in section 2 is answered or explicitly left open with a fallback |
| 1 — Foundation | Project skeleton, config, Postgres schema + Alembic, encryption, account CLI (add/list/disable) | Accounts are added encrypted and listed |
| 2 — xweb + transport | Operation registry, layered TID, curl_cffi transport, parsers + fixtures | CLI can search, fetch a profile and a user's tweets with one account |
| 3 — Pool + gateway | Limit state, selection, error classification, cache/coalescing, P0/P1/P2 budget | Pool/budget tests pass; live load spreads evenly over all accounts |
| 4 — HTTP API (on-demand) | Search/users/tweets endpoints, health, accounts, budget, OpenAPI | Bots can call it; 503 with retry_after works |
| 5 — Watchlist + feed | Scheduler, `from:` batching, capacity control, feed with long-poll | Tier-1 KOL tweets reach the feed within interval + 1 min |
| 6 — Canary + resilience | Canary, upstream watch, retention jobs, 24 h soak with 6–10 accounts | 24 h soak with no locked account; canary detects a simulated break |
| 7 — Consumer integration | zetryn migration, cdexio client, docs for the stock bot | Submitted separately for approval (touches other projects) |

## 14. Risks

- X changes (TID, queryIds, schema) — mitigated by layered fallbacks,
  canary, samples, upstream watch; fixes are applied by us.
- Account suspension — dedicated accounts only, headroom rule, pacing,
  browser-exported cookies, optional per-account proxy.
- Per-IP limits without proxies — detected through Cloudflare/429 counters
  across accounts; remedy is static proxies per account.
- Legal/ToS — scraping violates X's terms; volumes stay modest and the
  service is private.
