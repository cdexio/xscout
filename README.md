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
