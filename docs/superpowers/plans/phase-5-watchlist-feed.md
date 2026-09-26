# Phase 5 — Watchlist + feed

Goal: one scheduled poll serves every bot; bots read new tweets from a
local feed instead of spending X requests.

## Tasks

### 5.1 Watch items
- CRUD at `/v1/watchlist` with `kind`, `value`, `interval_sec`, `tags`,
  `enabled`; normalization of usernames and query text; merge of identical
  values from different bots (shortest interval, merged tags).
- Capacity check on create/update: projected requests per 15 min against
  the P1 share; `409` with needed vs available.

### 5.2 Scheduler
- Loop that picks due items by next-run time, runs them as P1 jobs through
  the gateway, and reschedules; skips and reschedules cleanly when the
  budget refuses.
- User watches grouped by interval class into `from:a OR from:b …`
  Latest searches, batch size from phase 0; fallback to UserTweets per
  user when a batch errors.
- Query watches as Latest searches with a `since`-style filter derived
  from the newest seen tweet.
- Dedup by tweet id per item.

### 5.3 Feed
- Every new tweet gets a monotonic sequence number and links to the items
  (and tags) that found it.
- `GET /v1/feed?tags=&since=&limit=&wait_sec=` returns entries after
  `since`, ordered, with `next_since`; long-poll up to 30 s on the local
  DB with a notification from the scheduler.

## Verification
- Unit tests: due-item selection, batching and fallback, capacity math,
  merge rules, dedup, feed ordering and long-poll wake-up.
- Live: a watchlist of tier-1 KOL accounts at 60 s interval; a new post
  from one of them appears in the feed within interval + 1 min; budget
  stays inside the P1 share.
