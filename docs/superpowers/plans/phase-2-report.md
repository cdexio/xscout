# Phase 2 report — xweb + transport (2026-09-26)

## Built

- `transport/session.py`: one curl_cffi session per account (fixed
  impersonation profile, optional proxy, own cookie jar). `x-csrf-token`
  is taken from the jar at send time, so a `ct0` rotated by X through
  Set-Cookie is picked up (`ct0_rotated` exposes it for persistence in
  phase 3). Network failures raise `TransportError`; X errors are never
  interpreted here.
- `xweb/constants.py`: bearers, `Bucket` (method × bearer) with the TID
  rules measured in phase 0 (GET/main needs a TID; alt-bearer requests send
  none), verified field-toggle values per operation.
- `xweb/discovery.py`: script-list parsing (legacy hash maps and `/x-web/`
  assets), page feature config, operation extraction for both known bundle
  patterns with each operation's feature switches; walks `main.js` first,
  then imports, until all wanted operations are found.
- `xweb/registry.py` + `store/operations.py`: fallback chain
  discovery → DB (last good, with history per query id) → packaged
  `fallback_ops.json`; error-336 self-heal persisted to the DB.
- `xweb/tid.py`: vendored key derivation (MIT attribution in the module
  and `THIRD_PARTY_NOTICES.md`); `TidProvider` with a per-account generator
  cache and rebuild interval, a 5-minute back-off after a failed build,
  and the pair-dict fallback.
- `xweb/requests.py`, `xweb/ops.py`: GET/POST request building per bucket;
  variables for search (latest/top/people, 512-character guard), user by
  screen name, user tweets.
- `xweb/errors.py`: X error codes from any response, including errors
  inside HTTP 200; rate headers; HTML detection.
- `xweb/models.py`, `xweb/parse.py`: stable `Tweet`, `User`, `Media`,
  page and parse-stats models; tolerant parsers (new and old schema
  locations, note tweets for long posts, visibility wrapper, tombstones,
  pinned entries, conversation modules, promoted and who-to-follow
  skipped, retweeted/quoted originals in `includes`, per-field null
  counts for the canary).
- `xweb/client.py`: one call on one account with wire-level self-healing
  (336 → heal and retry once; 404 on a TID bucket → rebuild TID and retry
  once) and response samples on parse problems (`store/samples.py`).
- Developer CLI: `xscout x discover|search|user|user-tweets`.

## Verified

- 67 tests pass (unit + DB), ruff check and format clean.
- Live with the stored account (encrypted row confirmed as Fernet
  ciphertext):
  - discovery: all 5 operations from one bundle, stored in `operations`;
  - `$BTC` search, 2 pages via POST/main: 39 unique tweets, no null key
    fields, quota 185/187;
  - people search "solana": 20 users;
  - profile `elonmusk` (followers, following, tweet count, blue verified);
  - `VitalikButerin` tweets: 20, 6 retweets detected;
  - unknown user → `null`.
  - TID layer `generator` accepted on every call.

## Notes

- Ruff formats multi-exception handlers in the Python 3.14 style
  (`except A, B:`, PEP 758); the project requires Python ≥ 3.14.
