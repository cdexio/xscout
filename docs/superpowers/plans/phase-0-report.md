# Phase 0 report — Live verification (2026-09-26)

One dedicated account, one residential IP, curl_cffi `chrome` impersonation.
About 40 GraphQL calls in total; no account went below 10 remaining; no lock,
no suspension, no Cloudflare block. Probe code: `tools/probe/`
(`discover.py`, `calls.py`, `buckets.py`, vendored `xclid.py`). Raw results
in `tools/probe/out/` (git-ignored); sanitized fixtures in
`tests/fixtures/x/`.

## Answers to the open items in spec §2

| Item | Result |
|---|---|
| Python 3.14 + dependencies | **OK.** curl_cffi 0.16.3, FastAPI 0.141.1, SQLAlchemy 2.1.1, asyncpg 0.31.0, Alembic 1.20.0, cryptography 50.0.1, pydantic-settings 2.15.0 all install and import on 3.14.7. No Python downgrade needed. |
| Logged-in page | `x.com/home` with cookies serves the logged-in **legacy `responsive-web` build** (1002 chunk URLs, `main.<hash>.js`), not the `/x-web/` build. The page includes `twitter-site-verification`, the `loading-x-anim` SVG and 1399 feature-switch values in `__INITIAL_STATE__`. The `cdn-cgi/challenge-platform/.../jsd` script is Cloudflare telemetry present on every page, not an interstitial. |
| queryId discovery | **Works from `main.js` alone**: 105 operations with the `queryId…operationName` pattern, each with its `featureSwitches` and `fieldToggles` lists. Current ids differ from twscrape's hardcoded ones (e.g. SearchTimeline `auLkqtmHqYEpRvflfvLhyQ` vs `hyPfJYJ_XAtDYoslQc-Rgg`), and both work, so X serves more than one valid id at a time. Feature values resolve from the page config; one switch (`rweb_conversational_replies_downvote_enabled`) is missing from the config and defaults to false without error. |
| TID | Own generator (from `ondemand.s.<hash>.js`) **accepted**. Pair-dict key pairs (5 pairs) **accepted**. Without a TID, **GET SearchTimeline returns 404**; UserByScreenName works without a TID. |
| Limits per operation | UserByScreenName **150**, SearchTimeline **50** (GET), UserTweets **50**, per 15 minutes. |
| **Limit buckets** | **Key finding.** SearchTimeline has **four independent buckets per account**, all returning full 20-tweet pages: GET + main bearer 50, **POST + main bearer 187**, GET + alternative bearer 50, POST + alternative bearer 187. The alternative bearer also has its own UserByScreenName bucket (150). POST does not need a TID, with either bearer. Theoretical search capacity is ~474 requests per 15 min per account instead of 50. |
| GET vs POST | Both work for SearchTimeline. POST has the bigger bucket. |
| Search query length | **Max 512 characters.** Over the limit, X answers **HTTP 200** with error code **214** ("Raw query length 984 exceeds max allowed 512") and no entries. A 490-character query with 26 `from:` terms works and still matches the last term. |
| `from:` batching | 20 real handles in one Latest query → 20 tweets from 7 distinct authors: busy accounts crowd out quieter ones in a single page. Batching works, but correctness needs a since-id watermark per poll plus paging until the watermark is reached. |
| Response schema | User objects have **no `legacy` at all**: `core` (name, screen_name, created_at), `relationship_counts` (followers, following), `tweet_counts`, `verification`, `profile_bio`, `avatar`, `location`, `privacy`. Tweets still carry `legacy` (full_text, counts, entities) plus `core.user_results`, `views`, `cashtag_attachments`. |
| Per-IP limit | **Still open.** Needs several accounts on one IP; measured in the phase 6 soak through Cloudflare/429 counters. |
| Hidden bot score from missing TID | **Still open** and not measurable directly. The fallback order puts TID-carrying requests first. |

## Consequences for the design

1. **Limit state is keyed by (account, operation, bucket)**, where bucket =
   method × bearer. The pool treats each bucket as its own quota and picks
   the bucket with the most headroom within the allowed set.
2. **Bucket policy (decision needed).** The main-bearer buckets are what the
   web client itself uses. The alternative-bearer buckets come from another
   official client token, a Nitter technique, and may look less natural.
   Proposed default: POST/main as primary, GET/main as secondary, with
   alternative-bearer buckets **enabled only as overflow** and switchable
   per account in config. The phase 6 soak decides whether overflow stays
   on.
3. **Errors inside HTTP 200** (e.g. 214) are classified from the body, not
   the status. 214 is an input error (never penalizes the account).
4. **Watch batching** caps queries at 500 characters (≈ 25–30 handles), and
   each poll pages until it reaches the newest tweet id already seen for
   that batch, with a page cap.
5. **Parsers** follow the new user schema first and keep the older
   locations as fallbacks (tolerant lookup).
6. Discovery reads the legacy build's `main.js` first and only walks other
   chunks if an operation is missing; `/x-web/` support stays for when X
   switches this account's build.

## Needs a decision from the owner

- Bucket policy in point 2: default above, or use all four buckets
  equally from the start.

## Security note

The probe account's cookies were shared in chat. After the probes, log that
session out in the browser (which invalidates this `auth_token`), then add
fresh cookies through the xscout CLI in phase 1.
