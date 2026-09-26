# Phase 1 — Foundation

Goal: a runnable project skeleton with configuration, the Postgres schema,
encrypted account storage and the account CLI.

## Tasks

### 1.1 Project skeleton
- uv project `xscout` in `x/`, Python version and dependency pins from
  phase 0, package layout matching spec §4 components (`api`, `gateway`,
  `pool`, `transport`, `xweb`, `scheduler`, `cache`, `store`, `canary`,
  `cli`).
- Tooling: ruff, pytest (with a `live` marker excluded by default),
  type checking.
- `.env.example` listing every variable without values.

### 1.2 Configuration
- Settings loaded from env + a YAML file: database URL, secret key, bind
  host/port, and all tunables from spec §5 (reserve, gap, P0/P1 shares,
  TTLs, cooldowns) with the documented defaults.
- Validation at startup; a missing secret key or database URL stops boot
  with a clear message.

### 1.3 Database
- Create databases `xscout` and `xscout_test` on the local server (owner
  provides or approves the role).
- Alembic migrations for the tables in spec §9.
- Test guard: DB tests refuse to run unless the URL points at
  `xscout_test`.

### 1.4 Secrets
- Fernet encryption helper for cookie fields; key from
  `XSCOUT_SECRET_KEY`; a CLI command that generates a new key.
- Log redaction filter for `auth_token`, `ct0`, `authorization`,
  `x-csrf-token`.

### 1.5 Account CLI
- `accounts add <username>`: hidden prompts for `auth_token` and `ct0`,
  optional proxy, assigns a fixed impersonation profile; stores encrypted.
- `accounts list`: status, proxy set or not, last error; never cookies.
- `accounts disable|enable <username>`, `accounts remove <username>`
  (confirmation required).

## Verification
- Unit tests: encryption round-trip, redaction, settings validation.
- DB tests: migrations up/down on `xscout_test`; account repository CRUD.
- Manual: add an account, list it, confirm the stored row is ciphertext.

## Owner inputs
- Postgres role/password for the `xscout` databases.
- Accounts' cookies, added by the owner through the CLI.
