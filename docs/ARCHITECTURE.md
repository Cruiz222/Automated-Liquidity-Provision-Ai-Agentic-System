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
5. `payouts`: NUBAN check, bank directory, name enquiry (pluggable) and saved payout accounts.
6. `accounts`: users, sessions, TOTP 2FA, email codes, KYC tiers, rate limiting, audit.
7. `service.PlatformService`: every customer operation, scoped to one `user_id`, with all limits and ownership checks.
8. `api`: HTTP transport and the dashboard. It validates and translates; it holds no business rules.

## Funds modes

`PlatformService` holds two **books** (`demo`, and `live` only when enabled). A book is a ledger, conversion quotes,
positions and a portfolio manager. The current book is selected per request through a context variable set by
`use_mode`, so all existing service methods work unchanged on either set of books; live mode is refused unless every gate
in `live_requirements` passes. Identity data (users, sessions, KYC, payout accounts, pool snapshots) lives beside the demo
books and is shared. See `docs/LIVE_MODE.md`.

## Market data

`dex/defillama.py` reads DefiLlama's public pool list, prices and price history, filters to a configured universe (chains,
AMM projects, known tokens, minimum TVL), and attaches USD prices. A pool without confident, current prices carries none
and is excluded rather than guessed. Position valuation (paper) is the principal scaled by sqrt(r0 x r1) from each token's
USD price change since entry, plus fees accrued in increments at the pool's current fee APR. Run `python3 -m app.feedcheck`
to verify the live feeds from a machine with internet access.

## Decisions worth knowing

* Ledger accounts are opaque user ids, never usernames. System accounts are reserved names.
* Money is integers in minor units (kobo, micro-USDT); floats appear only in analytics.
* Position valuation is simulated: value = principal adjusted for impermanent loss from the pool price change, plus
  fees accrued at the pool's modelled APR over simulated time (`sim_time_scale`, default one real minute = one
  simulated day). Token price moves against USD are not modelled. Marks are bounded to 2x principal.
* Pool eligibility uses the latest stored snapshot and the current time; stale or incomplete data excludes a pool.
* Pools are modelled as full-range exposure; concentrated-liquidity range management is not modelled.

## Not built yet

Real data providers (rates, pools, bank list and name enquiry), on-chain valuation and execution, custody, a real KYC
provider and compliance, encryption of secrets at rest, admin and support tooling, background jobs for snapshots and
reconciliation, and a production database.
