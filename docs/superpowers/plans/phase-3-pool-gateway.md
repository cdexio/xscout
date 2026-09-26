# Phase 3 — Pool + gateway

Goal: every request to X goes through one gateway that picks accounts
fairly, stops before quota runs out, classifies errors, shares results
through cache and coalescing, and respects the P0/P1/P2 budget.

## Tasks

### 3.1 Limit state
- Per (account, operation) state from response headers; conservative
  defaults for unseen operations; window rollover at `reset_at`.
- Persisted in `rate_limit_state` so a restart does not forget cooldowns.

### 3.2 Account selection (lease)
- Eligibility and ordering per spec §5 (status, cooling, in-flight cap,
  reserve rule, highest remaining then least recently used).
- Minimum gap with jitter per account.
- A lease is released with the response outcome; leases time out if a
  request hangs.

### 3.3 Error classification
- Map status, X error codes and HTML/Cloudflare bodies to the actions in
  spec §5; account status transitions and warnings; retry on another
  account only where the table allows it, with a bounded retry count.

### 3.4 Budget
- Capacity per operation from usable accounts; P0/P1/P2 shares; P1 may
  borrow unused P0; per-consumer usage counters.
- A request that cannot be admitted gets an estimated `retry_after_sec`.

### 3.5 Cache and coalescing
- Result cache keyed by operation + normalized variables, TTL per
  operation, `max_age_sec` override, stale lookup for the 503 path.
- In-flight map so identical jobs share one X request.

### 3.6 Gateway API (internal)
- One entry point: run(operation, variables, pages, priority, consumer,
  max_age) → Page plus meta (cached, stale, age, pages fetched), or an
  Unavailable error with retry-after.
- Request log row per X request (consumer, operation, account, status,
  latency).

## Verification
- Unit tests with a fake clock and fake transport: fair spread over N
  accounts, reserve respected, every error class → expected transition,
  budget shares and borrowing, coalescing, stale serving.
- Live (`-m live`, all accounts): a burst of searches spreads within ±1
  request across accounts; no account below the reserve.
