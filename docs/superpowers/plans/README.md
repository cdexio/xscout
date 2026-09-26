# xscout implementation plans

One plan per phase. Plans are **functional**: they say what each unit does,
its inputs, outputs, dependencies and how it is verified. They contain **no
full code blocks** (project rule 3); code is written only while a phase is
implemented, following its plan. Design:
`../specs/2026-09-26-xscout-design.md`.

| Phase | Plan | Depends on | Exit criterion |
|---|---|---|---|
| 0 | [Live verification](phase-0-live-verification.md) — **done 2026-09-26**, see [report](phase-0-report.md) | — | every `[U]` in spec §2 answered or left open with a fallback; fixtures recorded |
| 1 | [Foundation](phase-1-foundation.md) | 0 | accounts added encrypted and listed; DB migrated |
| 2 | [xweb + transport](phase-2-xweb-transport.md) | 1 | CLI searches, fetches a profile and a user's tweets with one account |
| 3 | [Pool + gateway](phase-3-pool-gateway.md) | 2 | pool/budget tests pass; live load spreads evenly over all accounts |
| 4 | [HTTP API (on-demand)](phase-4-http-api.md) | 3 | bots can call it; 503 with retry_after works |
| 5 | [Watchlist + feed](phase-5-watchlist-feed.md) | 4 | tier-1 KOL tweets reach the feed within interval + 1 min |
| 6 | [Canary + resilience](phase-6-canary-resilience.md) | 5 | 24 h soak, no locked account; canary detects a simulated break |
| 7 | [Consumer integration](phase-7-consumer-integration.md) | 6 | submitted separately for owner approval |

Each phase ends with a short report to the owner
(`phase-N-report.md`): what was built, what was measured, what needs a
decision, and proposed solutions for any problem found.
