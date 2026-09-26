# Phase 5 report — Watchlist + feed (2026-09-26)

## Built

- `scheduler/units.py`: normalization (usernames lowercased, query
  whitespace collapsed, 500-character guard), batching of user watches per
  interval into `(from:a OR from:b …)` queries of at most 500 characters,
  projected requests per 15-minute window.
- `scheduler/scheduler.py`: tick loop (1 s) over due units, most overdue
  first, at most 4 concurrent polls, no unit polled twice at once; each poll
  runs page by page through the gateway as P1 with `max_age_sec=0` and stops
  at the lowest watermark of the unit (page cap 5). After a scan every item
  of the unit gets watermark = newest id fetched, so quiet accounts do not
  force deep paging. First run of an item only emits tweets created since
  `created_at − interval`. Rejected batch (400) → per-user `UserTweets`
  fallback. Unavailable → retry after `min(interval, max(5, retry_after))`.
  `FeedSignal` wakes long-polling readers.
- Migration `0002`: `feed_entries.payload` (normalized tweet JSON) and an
  index on `watch_item_id`.
- `store/watch.py`: watch items (merge on create: shortest interval, tag and
  consumer union; `detach` removes a consumer and deletes the item when none
  is left; run updates never lower a watermark) and the feed (dedup per
  tweet and item, tag filter with array overlap).
- `api/watch.py` + endpoints: `POST/GET/PATCH/DELETE /v1/watchlist`,
  `GET /v1/watchlist/capacity`, `GET /v1/feed` with long-poll ≤ 30 s.
  Capacity check against the P1 share of a full window of all active
  accounts (temporary cooldowns ignored so bots can register while accounts
  rest); `409 over_capacity` with needed vs available.
- Runtime runs the scheduler; `/health` reports scheduler counters.
- `gateway.max_wait_sec_p1` default raised from 0 to 5 s so watch polls
  wait briefly for a free account instead of failing on the per-account gap.

## Verified

- 140 tests pass (unit + DB), `alembic check` clean after `0002`.
- Live, about 20 minutes, one account:
  - watch registration, merge of the same user from two consumers
    (interval 60 wins, tags and consumers united), interval validation,
    capacity report (22.5 of 256.8 per 15 min);
  - `$BTC` query watch every 120 s: 21 feed entries, delay from tweet
    creation to feed at most 129 s (interval + 9 s), long-poll woke as soon
    as entries arrived;
  - user batch of 10 accounts (191 characters): 28 polls, 0 errors, correct
    authors returned, watermarks equal to the newest available tweet;
  - no new tweets were posted by the watched accounts during the test
    (Saturday; the most active one last posted 18 minutes before), so no
    user-watch entry could appear yet. Selection per author, first-run
    filter and dedup are covered by unit tests; the live user path is
    re-checked in the phase 6 soak.
  - Test watch items were removed afterwards.

## Open

- A live observation of a new KOL post reaching the feed, during the phase
  6 soak (market hours).
