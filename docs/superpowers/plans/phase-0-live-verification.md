# Phase 0 — Live verification

Goal: replace the uncertain facts in spec §2 with measured ones before any
production code is written, and record real responses to use as fixtures.
Everything here is probe tooling in `tools/probe/`; it is kept only as
reference and fixture source, not as the service code.

## Tasks

### 0.1 Environment
- Verify curl_cffi, FastAPI, SQLAlchemy async, asyncpg, Alembic and
  `cryptography` install and import on the machine's Python (3.14). If a
  wheel is missing, pin the newest Python that has all wheels (via uv) and
  record the choice.
- Record the chosen versions for phase 1.

### 0.2 Single-account session
- One dedicated account's cookies, read from a local, git-ignored file for
  the probe only.
- curl_cffi session with a fixed Chrome impersonation profile; headers per
  spec §6 (bearer, csrf from `ct0`, auth type, active user, referer).
- Confirm the session is logged in (a lightweight viewer call).

### 0.3 Operation discovery
- Fetch `x.com/home`, collect script URLs, follow `import()` references,
  extract queryId/operationName pairs with both known patterns and default
  feature flags.
- Compare against twscrape's current constants; record differences.

### 0.4 TID
- Vendored generator: build keys from pages fetched through the account's
  session; call SearchTimeline and UserByScreenName with and without a TID;
  record status codes.
- Check the `fa0311/x-client-transaction-id-pair-dict` source format and
  that a TID built from it is accepted.
- Check the no-TID mode with the alternative bearer token.

### 0.5 Limits and methods
- Call SearchTimeline, UserByScreenName, UserTweets, SearchTimeline
  (People) a few times each; log `x-rate-limit-*` headers. Do not drive any
  counter below 10.
- SearchTimeline via GET and via POST; record which works.
- Maximum search query length: grow a `from:a OR from:b …` query until X
  rejects or silently truncates; record the safe batch size.

### 0.6 Fixtures
- Save sanitized responses (cookies, csrf and account identifiers removed)
  for each operation, including a pagination cursor page and an empty
  result, under `tests/fixtures/x/`.

## Verification
- A phase-0 report with a table: each `[U]` item → measured answer or
  "still open" plus the fallback chosen.
- No account reached `remaining` below 10; no lock or suspension.

## Owner inputs
- Cookies of one dedicated X account, placed by the owner in the local
  probe file (never pasted into chat).
