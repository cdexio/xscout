# Phase 7 — Consumer integration

Goal: the three bots use xscout. This phase changes other projects, so it
starts only after the owner approves a concrete change list per bot.

## Tasks

### 7.1 zetryn (TypeScript)
- Typed client generated from `docs/openapi.json` (or a thin hand-written
  client) replacing `TwitterSidecarClient`.
- Move the sidecar's `aggregate()` scoring (mentions 1h/24h, unique
  authors, weighted score, KOL posted) into the zetryn codebase, fed by
  `/v1/search/tweets`.
- Keep the existing `unavailable` semantics: xscout's 503 with
  `retry_after_sec` maps to the current back-off path.
- KOL list moves to an xscout watchlist tagged `zetryn`; zetryn reads the
  feed.
- Retire the twscrape sidecar after a side-by-side comparison.

### 7.2 cdexio (Python)
- Small async client module; watchlist items tagged `cdexio` for the
  accounts and keywords the owner chooses; feed consumption where the
  strategy needs it.

### 7.3 Stock bot
- Consumer guide in the xscout README: endpoints, envelope, feed pattern,
  budget etiquette (`max_age_sec`, tags), error handling.

## Verification
- Per bot: its existing test suite passes; a live run shows data flowing
  from xscout and per-consumer usage in `/v1/budget`.
- zetryn: side-by-side run of old sidecar and xscout gives comparable
  mention numbers for the same tokens.

## Owner inputs
- Approval of each bot's change list before work starts.
- KOL accounts and keywords per bot.
