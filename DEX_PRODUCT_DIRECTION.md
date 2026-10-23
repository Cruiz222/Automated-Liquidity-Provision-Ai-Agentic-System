# Product direction: DEX liquidity provision, not market making

## Product definition

Build an AI-assisted product that helps users fund NGN/USDT wallets, convert between NGN and USDT, select decentralized exchange automated-market-maker (AMM) pools, allocate capital, monitor liquidity positions and earned fees, and withdraw funds or close positions.

The product does **not** continuously place limit bids and asks on a centralized order book. Spread capture, inventory-skewed quoting, exchange fill simulation and order-book market-making are not product features. Existing market-maker research modules are legacy tools and should be isolated from the DEX product path, not extended as its core.

## Scope and safety boundaries

- Every operation in the current milestone is simulated/local. “Deposit” does not mean money was received; “withdrawal” does not mean a bank payout or blockchain transfer occurred.
- NGN is represented in kobo; USDT in micro-USDT. Do not use floating-point values for wallet accounting.
- Pool data must include source, observed-at timestamp, chain, DEX, pool address, token addresses, fee tier, TVL, volume, liquidity depth, price, and data freshness before a live adapter is trusted.
- A pool score is a screening aid, not an expected return. It must not override hard safety gates.
- LP risk analysis must account for impermanent loss/divergence loss, fee variability, token price movement, depeg, contract/oracle/bridge risk, slippage, gas, withdrawal liquidity and incentives.
- Never execute a live transaction automatically from an AI score. Production requires explicit authorization, transaction simulation, spending limits, allowlists, monitoring and a reviewed key-management model.

## Architecture target

1. `WalletLedger`: immutable simulated accounting entries and idempotent user operations.
2. `ConversionQuotes`: short-lived NGN↔USDT quotes with explicit fees and idempotent execution.
3. `PoolDataProvider` (future): validated, timestamped DEX pool snapshots from named sources.
4. `dex_pool_advisor`: explainable offline screening and bounded allocation recommendations.
5. `LiquidityPositions`: LP position lifecycle and accounting, later expanded to chain/pool-specific token amounts and fee data.
6. API/auth/UI (future): authenticated users, strict account isolation, wallet activity, pool rankings, position details and explicit approval flows.

## Milestone 3 delivered in this patch

- Added idempotent simulated wallet withdrawals with insufficient-balance protection.
- Added an explainable DEX pool screening baseline with hard exclusion rules.
- Added bounded capital-allocation recommendations that never submit transactions.
- Added tests for idempotency, insufficient balances, input validation, risk gates and allocation limits.
- Updated README to describe the DEX-first product and mark market-making components as legacy research.

## Still not included

No live market/pool feed, bank transfer, KYC, user authentication, production API, wallet custody/signing, chain RPC, DEX router, token approvals, real conversion execution, AMM position math, impermanent-loss model, or production-grade accounting reconciliation.
