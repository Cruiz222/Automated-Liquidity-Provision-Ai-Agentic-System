# Progress report — Milestones 6–7

## Added
- Account-scoped product service wrapping the simulated wallet ledger.
- SQLite persistence for DEX LP position records with ownership-scoped retrieval and close operation.
- Local-only JSON HTTP API for registration/login/logout, wallet balances/history, simulated deposits/withdrawals, and position records.
- PBKDF2 password hashing, random bearer tokens, token expiry, request size checks, idempotency-key requirement for wallet mutations, and tests.

## Not production-ready
- Authentication users and sessions live in memory and reset on restart.
- No TLS, rate limiting, lockout, CSRF concerns are addressed for browser deployment, secret management, or external identity provider.
- Simulated deposit/withdrawal endpoints do not interact with banks or blockchains.
- LP position records do not create or manage actual on-chain liquidity.
- Conversion quotes and live DEX data are not wired into this API.

## Next
Persist authentication securely; add a mature production web framework and migrations; implement audited NGN/USDT quote/confirm service; connect read-only live DEX adapters; model positions against pool-specific AMM math before any transaction execution.
