# xscout

Self-hosted X (Twitter) search service shared by the trading bots (cdexio,
zetryn, the stock bot). It logs in with browser cookies of dedicated X
accounts, calls X's web GraphQL API, and keeps every account below its limits
with a shared pool, budget and cache. Loopback only.

Design: `docs/superpowers/specs/2026-09-26-xscout-design.md`. Plans and phase
reports: `docs/superpowers/plans/`.

## Setup

```bash
uv sync                                # Python 3.14, creates .venv
cp .env.example .env                   # fill in the database URLs
uv run xscout keygen --write-env       # encryption key for stored cookies
uv run alembic upgrade head            # create tables
uv run pytest -q                       # unit + DB tests (live tests are opt-in: -m live)
```

Optional tunables: copy `config/xscout.example.yaml` to `config/xscout.yaml`.

## Accounts

Use dedicated accounts only; scraping is against X's terms and accounts can
be locked. Never paste cookies into chat or commit them.

1. Log in to x.com in a browser, open DevTools → Application → Cookies →
   `https://x.com`, copy `auth_token` and `ct0`.
2. `uv run xscout accounts add <username>` and paste `auth_token=...; ct0=...`
   at the hidden prompt (`--with-proxy` to also set a proxy).
3. `uv run xscout accounts list` / `uv run xscout pool` (quota per bucket).

A running service picks up new or replaced accounts within 60 seconds.
Status meanings: `locked` → open the account in a browser and solve the
unlock; `expired` → add fresh cookies with `accounts add <user> --replace`;
`suspended` → replace the account.

## Running

```bash
uv run xscout serve                    # foreground, 127.0.0.1:8790
pm2 start ecosystem.config.cjs         # background; pm2 logs xscout
curl -s localhost:8790/health | jq
```

`/health` → `status` is `ok`, `degraded` (with `problems`) or `down` (no
usable account).

## API for the bots

Every request needs `X-Consumer: <bot name>` (`cdexio`, `zetryn`, `stocks`).
Schema: `docs/openapi.json` (regenerate with `uv run xscout openapi`).

| Endpoint | Purpose |
|---|---|
| `GET /v1/search/tweets?q=&tab=latest\|top&limit=&cursor=&max_age_sec=` | Tweet search; X operators allowed (`$BTC`, `from:`, `since:`, `min_faves:`), max 512 characters |
| `GET /v1/search/users?q=&limit=&cursor=` | User search |
| `GET /v1/users/{username}` | Profile |
| `GET /v1/users/{username}/tweets?limit=&cursor=` | Latest tweets (no replies) |
| `GET /v1/accounts`, `GET /v1/budget`, `GET /health` | Status |

Response: `{"data": [...], "includes": [...], "next_cursor": "...", "meta":
{"cached", "stale", "shared", "age_sec", "fetched_at", "pages_fetched"}}`.
`includes` holds the original tweets of retweets and quotes.

Errors: `{"error": {"code", "message", "retry_after_sec"?}}` with `400`
(invalid input), `404` (unknown user), `503` + `Retry-After` (no quota right
now). Treat `503` as "no X data for now", never as a signal.

### Watchlist and feed

Instead of asking X repeatedly, register what you want watched; one scheduled
poll serves every bot, and bots read new tweets from the local feed.

| Endpoint | Purpose |
|---|---|
| `POST /v1/watchlist` `{"kind": "user"\|"query", "value", "interval_sec", "tags": []}` | Watch a user or a search query (interval 30 s – 24 h). The same value from two bots is merged (shortest interval, tags and consumers united). `201` created, `200` merged, `409 over_capacity` with needed vs available. |
| `GET /v1/watchlist?mine=true&tag=` | List items |
| `GET /v1/watchlist/capacity` | Projected requests per 15 min vs the watchlist share |
| `PATCH /v1/watchlist/{id}` `{"interval_sec"?, "tags"?, "enabled"?}` | Change an item |
| `DELETE /v1/watchlist/{id}` | Remove your interest; the item is deleted when no bot uses it |
| `GET /v1/feed?tags=a,b&since=<seq>&limit=&wait_sec=` | New watched tweets after `since`, oldest first, with `next_since`; `wait_sec` (≤ 30) long-polls |

Feed pattern: keep the last `next_since`, call
`/v1/feed?tags=<yours>&since=<it>&wait_sec=30` in a loop. Each entry has
`seq`, `tags`, `watch_item_id` and the normalized `tweet`. On the first poll of
an item only tweets from about one interval before it was created are sent,
not the account's history.

Etiquette that keeps everyone under the limits:

- Pass `max_age_sec` as large as your strategy allows; identical queries from
  different bots are then served from one X request.
- Keep `limit` small (20 = one X request).
- Back off for `retry_after_sec` after a `503`.

## Developer commands

`uv run xscout x search '$BTC'`, `x search solana --tab people`,
`x user elonmusk`, `x user-tweets VitalikButerin`, `x discover`,
`x burst q1 q2 --repeat 2` spend real quota on one account (or the gateway
for `burst`); use them sparingly.
