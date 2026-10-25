# Architecture

## Layers (each depends only on those above it)

1. `ledger`: SQLite double-entry ledger. Operations are idempotent, entries immutable, customer balances non-negative.
   Deposits (instant or provider-confirmed), conversions, and withdrawals (`pending, processing, completed` or
   `failed`/`cancelled` with full refund), per-currency limits and fees, `history()`, `reconcile()`.
2. `fx`: `RateService` (bid/ask, staleness limit, multi-source median, deviation guard) and `ConversionQuotes`
   (30-second single-use quotes owned by one account, rate provenance, fee booked to the `fees` account).
3. `dex`: `PoolSnapshot` data, `SnapshotStore`, `FixturePoolProvider`, exact IL math (`il.py`), a Decimal AMM calculator
   (`amm.py`), and `PoolSelector` (fee APR vs expected IL vs depeg, risk score, per-category policy, caps, reserve,
   gas-aware exclusion).
4. `portfolio`: `LiquidityPositions` (ledger-backed, audited marks) and `PortfolioManager` (valuation, deployment).
5. `accounts`: users, sessions, rate limiting, audit.
6. `service.PlatformService`: every customer operation, scoped to one `user_id`, with all limits and ownership checks.
7. `api`: HTTP transport and the dashboard. It validates and translates; it holds no business rules.

## Decisions worth knowing

* Ledger accounts are opaque user ids, never usernames. System accounts are reserved names.
* Money is integers in minor units (kobo, micro-USDT); floats appear only in analytics.
* Position valuation is simulated: value = principal adjusted for impermanent loss from the pool price change, plus
  fees accrued at the pool's modelled APR over simulated time (`sim_time_scale`, default one real minute = one
  simulated day). Token price moves against USD are not modelled. Marks are bounded to 2x principal.
* Pool eligibility uses the latest stored snapshot and the current time; stale or incomplete data excludes a pool.
* Pools are modelled as full-range exposure; concentrated-liquidity range management is not modelled.

## Not built yet

Real data providers (rates and pools), on-chain valuation and execution, custody, KYC and compliance, email and 2FA,
admin and support tooling, TLS-terminated deployment, background jobs for snapshots and reconciliation, and a
production database.
