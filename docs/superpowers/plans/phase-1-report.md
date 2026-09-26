# Phase 1 report — Foundation (2026-09-26)

## Built

- uv project `xscout` (Python 3.14, `src/` layout) with one package per spec
  §4 component; ruff (line length 120), pytest with `live` (opt-in) and `db`
  markers.
- `config.py`: secrets and endpoints from env/`.env` (`XSCOUT_` prefix,
  async driver and non-empty key enforced), tunables from
  `config/xscout.yaml` with spec defaults, including the approved bucket
  policy (POST/main → GET/main → overflow POST/alt, GET/alt) and the
  phase 0 limit defaults. Example file: `config/xscout.example.yaml`.
- `crypto.py`: Fernet `SecretBox`; wrong key fails loudly.
- `log.py`: JSON logs; redaction of registered secrets and of
  `auth_token`/`ct0`/`x-csrf-token`/`authorization` fragments.
- `store/`: async engine, ORM models for the ten spec §9 tables
  (rate-limit state keyed by account, operation and bucket), Alembic env
  and hand-written migration `0001`.
- `store/accounts.py`: repository (add/replace, list without secrets,
  decrypt credentials, status, overflow flag, remove), username and cookie
  validation, fixed impersonation profile per account.
- CLI `xscout`: `keygen [--write-env]`, `db check`,
  `accounts add|list|disable|enable|overflow|remove`. Cookies and proxy are
  read from hidden prompts only.
- Databases `xscout` and `xscout_test` (owner `xscout`) created and
  migrated.

## Verified

- 24 tests pass (19 unit, 5 DB on `xscout_test`), including migration
  down/up round trip and the guard that refuses any other database.
- `alembic check`: models and migrated schema match.
- `xscout db check` → revision 0001.

## Open

- Owner adds accounts with `uv run xscout accounts add <username>`, after
  logging out the probe session whose cookies went through chat. Then the
  manual check "stored row is ciphertext" is run on a real account.
