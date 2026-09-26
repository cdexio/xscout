# Phase 2 — xweb + transport

Goal: one account can make correct, browser-like GraphQL calls and get
normalized `Tweet`/`User` data back, with every fragile piece isolated in
`xweb`.

## Tasks

### 2.1 Transport
- Per-account curl_cffi async session: fixed impersonation profile, optional
  proxy, cookie jar from the decrypted account, keep-alive.
- Header builder per spec §6; client language configurable.
- Returns status, headers (including `x-rate-limit-*`), body and timing to
  the caller; never interprets X errors (that is the gateway's job).

### 2.2 Operation registry
- Discovery from x.com bundles (both patterns), stored in the `operations`
  table with history; refresh at startup, every 6 h, and on demand.
- Fallback chain: last good discovery → hardcoded constants.
- Per-operation method (GET/POST) and feature flags; self-heal hook for
  error 336 that adds the requested flags and persists them.

### 2.3 TID provider
- Vendored generator (with MIT attribution in the module header and
  `THIRD_PARTY_NOTICES.md`), one instance per account, cached with a
  rebuild interval.
- Fallback 1: key pairs from `fa0311/x-client-transaction-id-pair-dict`,
  refreshed hourly.
- Fallback 2: no-TID mode with the alternative bearer; reported as
  degraded.
- The provider exposes which layer is active, for health.

### 2.4 Parsers and models
- Models `Tweet`, `User`, `Page` with the v1 fields in spec §6.
- Tolerant field lookup across old and new schema locations; unparseable
  entries skipped and counted; cursors extracted for pagination.
- Parsers for SearchTimeline (Latest, Top, People), UserByScreenName,
  UserTweets.
- Sample writer: stores the raw body on parse failure.

### 2.5 Developer CLI
- `x search`, `x user`, `x user-tweets` commands using one named account,
  printing normalized JSON. For development and live checks only.

## Verification
- Parser tests against phase-0 fixtures (including empty results and
  cursor pages), plus synthetic fixtures for both schema generations.
- Registry tests: discovery parsing on saved bundle snippets, fallback
  order, 336 self-heal.
- TID tests: deterministic output for fixed inputs; layer switching.
- Live (`-m live`): each CLI command returns data with one account.
