# Liquidity Lab: NGN/USDT wallet with DEX liquidity-provision screening

A Python platform where a customer registers, holds **NGN** and **USDT** wallets, converts between them with priced quotes,
withdraws to Nigerian bank accounts by account number, and places USDT into DEX liquidity pools chosen by a transparent
screening engine. It has a hardened JSON API and a browser dashboard.

> **No real money moves today.** Demo funds are simulated. A separate **live** set of books exists behind gates, but its
> payment and chain rails are a sandbox: there is no bank or blockchain adapter yet (see `docs/LIVE_MODE.md`). The server
> refuses to bind to anything but a loopback address.

## Two kinds of funds

* **Demo funds** (default): simulated deposits and payouts. With `DEX_DATA=live` they trade against **real pool data**
  (DefiLlama): positions are paper-traded, valued from real token prices and fee rates in real time, with nothing executed
  on-chain.
* **Live funds**: separate ledgers, available only after the operator enables them and the customer has verified identity,
  verified email, a PIN and accepted the terms. Deposits and payout results arrive by signed webhook. Only sandbox rails exist.

A banner always states which books you are in. Switch with **Demo funds / Live funds** in the header.

## Run it

Python 3.10+ and nothing else (standard library only).

```bash
python3 -m app                                  # dashboard at http://127.0.0.1:8010 (synthetic pools, demo funds)
DEX_DATA=live DEX_RATE_SOURCES=coingecko-tether-ngn python3 -m app    # demo funds on live pools and a real reference rate
python3 -m app.feedcheck --rates coingecko-tether-ngn                 # check the live feeds (needs internet)
python3 -m app.demo                             # command-line walkthrough
python3 -m unittest discover -s tests -t .      # tests
```

Verification and reset codes are printed to the server console and `data/outbox.log` unless SMTP is configured.
Other settings are in `docs/DEPLOYMENT.md`.

## DEX historical research data

Collect daily history (the default) into a dedicated SQLite database:

```bash
python3 -m app.dex.collect_cli --mode live \
  --database data/dex_snapshots_daily.sqlite3 \
  --history-period 1d --history-days 30
```

Collect approximately 30 days of hourly chart observations separately:

```bash
python3 -m app.dex.collect_cli --mode live \
  --database data/dex_snapshots_hourly.sqlite3 \
  --history-period 1h --history-points 720
```

Timestamped price observations are authoritative for volatility and stablecoin
depeg analysis. The estimator uses realized log-return quadratic variation per
unit of elapsed time and rejects gaps longer than the selector's configured
48-hour maximum. It also requires at least 14 days of timestamped coverage by
default. Older snapshots without timestamps retain the legacy daily-close
volatility fallback.

These observations are historical reference prices, not executable quotes.
OOS results remain modelled simulations; constant fee-APR assumptions are not
realized historical LP fee income.

## Security basics for customers

* A **transaction PIN** (6 digits) must be set before any transaction and is entered to confirm each one.
* Forgot your password? Reset it with a code sent to your verified email. (There are no recovery codes.)
* Optional authenticator-app code at login; if you lose your phone, a password reset by email turns it off.

## Layout

```
app/
  ledger/      double-entry NGN/USDT ledger: deposits, withdrawals, holds, fees, reconcile()
  fx/          rate sources and service (bid/ask, staleness, deviation guard), presets, conversion quotes
  dex/         pool snapshots and store, DefiLlama provider, safe fetcher, IL and AMM math, pool selector
  portfolio/   ledger-backed liquidity positions, valuation, allocation deployment
  payouts/     NUBAN check, bank directory, name enquiry, saved payout accounts
  accounts/    users, sessions, PIN, authenticator, email codes, KYC tiers, rate limiting, audit
  live/        rail and chain adapter interfaces, signed webhooks, sandbox adapters
  service.py   account-scoped business rules and the demo/live books (the only layer the API calls)
  api/         hardened HTTP server and the dashboard (static/)
  config.py  demo.py  feedcheck.py  sandbox_webhook.py  __main__.py
examples/      synthetic pool snapshot
docs/          ARCHITECTURE.md, SECURITY.md, PAYOUTS.md, DEPLOYMENT.md, LIVE_MODE.md
tests/         unit, service and HTTP security tests
```

## Meteora and Kamino Integration

The Automated Liquidity Provision (ALP) system is designed to interact programmatically with Solana-based DeFi protocols through their published SDKs, on-chain programs, and Solana RPC infrastructure.

The AI component provides market analysis and proposes liquidity-management decisions. A separate deterministic execution layer validates those decisions, enforces risk policies, constructs transactions, and submits approved operations.

### 1. Protocols and Developer Tooling

**Meteora DLMM**

Meteora's Dynamic Liquidity Market Maker (DLMM) uses discrete liquidity bins to provide concentrated liquidity.

Official TypeScript SDK:
- Package: `@meteora-ag/dlmm`
- Repository: https://github.com/MeteoraAg/dlmm-sdk
- Documentation: https://docs.meteora.ag/

Relevant documented SDK operations include:
- `DLMM.create()` — initialize a pool client.
- `getActiveBin()` — retrieve active-bin information.
- `getBinsAroundActiveBin()` — inspect liquidity around the active bin.
- `initializePositionAndAddLiquidityByStrategy()` — initialize a position and add liquidity.
- `addLiquidityByStrategy()` — add liquidity to an existing position.
- `removeLiquidity()` — remove liquidity from a position.

Exact method signatures and parameters must be checked against the SDK version selected for implementation.

**Kamino**

Kamino provides distinct products that must be integrated according to their intended responsibilities.

- Kamino Lending SDK: `@kamino-finance/klend-sdk`
- Kamino Liquidity SDK: `@kamino-finance/kliquidity-sdk`
- Lending SDK: https://github.com/Kamino-Finance/klend-sdk
- Liquidity SDK: https://github.com/Kamino-Finance/kliquidity-sdk

Kamino Lending can provide access to lending markets, reserves, deposits, and borrowing functionality.

Kamino Liquidity provides automated concentrated-liquidity strategies across supported protocols.

The ALP system must distinguish between lending capital and concentrated-liquidity positions. Integration with one product does not automatically manage positions in another.

**Solana infrastructure**
- `@solana/web3.js` for Solana connections and transaction infrastructure.
- Anchor tooling where required by the selected program SDK.
- A reliable RPC provider with HTTP and WebSocket support.

### 2. Integration Architecture

The system is divided into four logical responsibilities:

1. **Market Intelligence:** Collects token prices, trading activity, liquidity depth, volatility, pool fees, and protocol state.
2. **AI Strategy Engine:** Evaluates available opportunities and proposes capital allocation and liquidity-management decisions.
3. **Risk and Policy Engine:** Validates token identity, liquidity, slippage, exposure limits, protocol health, and strategy constraints.
4. **Execution and Signing:** Constructs, simulates, signs, submits, and monitors approved transactions.

The AI model must not have direct access to private keys or unrestricted transaction-signing authority.

### 3. Transaction Lifecycle

A proposed liquidity operation follows this lifecycle:

1. Collect current market and on-chain data.
2. Generate a proposed strategy.
3. Validate the proposal against deterministic risk policies.
4. Calculate permitted amounts, price limits, and slippage constraints.
5. Construct the protocol transaction using its official SDK.
6. Simulate the transaction and inspect expected effects.
7. Authorize and sign through an isolated signing boundary.
8. Submit the transaction and monitor its confirmation.
9. Reconcile the resulting on-chain state with internal accounting.

Failed, expired, or partially completed operations must be handled explicitly. The system must never assume a transaction succeeded merely because submission returned a signature.

### 4. Security and Execution Considerations

**Key management:** Keep signing credentials isolated from the AI model. Apply transaction authorization, spending limits, key rotation, and emergency shutdown procedures.

**RPC infrastructure:** Use reliable RPC infrastructure with appropriate rate limits, WebSocket subscriptions, retry policies, and fallback providers.

**Slippage and execution:** Calculate slippage limits dynamically and enforce maximum permitted price impact. Reject transactions outside the configured risk limits.

**MEV considerations:** Jito bundles can support sequential, atomic execution of multiple transactions. They may be useful for specific multi-step operations, but do not guarantee transaction inclusion or eliminate all execution risks.

**Protocol verification:** Validate program IDs, SDK versions, supported instructions, account requirements, and transaction simulation results before enabling live execution.

### 5. Integration Development Strategy

Protocol integrations should be developed incrementally:

- Begin with read-only pool and market-state collection.
- Implement and test transaction construction on devnet or with controlled test environments.
- Validate position accounting, deposits, withdrawals, and failure recovery.
- Introduce strict risk limits before enabling funded execution.
- Expand to automated capital movement only after the individual integrations have passed their security and operational tests.

All protocol interactions must be independently testable and isolated behind application-level interfaces.


Yes—programming your AI with balance-aware "capital tiers" is the exact right architecture. Instead of forcing a single strategy across every portfolio size, your agent can dynamically adjust its pool selection, price-range width, and rebalancing thresholds based on your current wallet balance.

Capital-Tiered AI Strategy Framework


Capital Tier	Target Pool Type	Price Range Strategy	Rebalance Trigger Rule
Micro Tier ($50 – $250)	High-fee trending meme pairs (1% – 5% fee tiers) or high-volume SOL pairs.	Wide ranges (±15% to ±30%) so the position earns fees for days without needing costly rebalances.	Only rebalance if unclaimed fees are at least 3x to 5x the total gas + slippage cost, or if price breaks out for >4 hours.
Growth Tier ($250 – $2,000)	Established mid-cap volatile pairs (e.g., JUP/SOL, WIF/SOL, BONK/SOL at 0.25% – 1% fee tiers).	Moderate ranges (±5% to ±15%) adjusted dynamically using 24-hour volatility (ATR).	Rebalance when unclaimed fees exceed 2x the rebalance cost, allowing 1–2 adjustments per day.
Scale Tier ($2,000+)	Deep-liquidity blue-chip pairs (SOL/USDC, SOL/USDT) alongside high-volume meme pools.	Tight, high-efficiency ranges (±1% to ±5%) to capture maximum fee share during sideways chop.	Frequent dynamic rebalancing (every 15–60 minutes during active volume) + optional delta-hedging on perps.


Key Rules to Code Into Your Balance-Aware Agent

• Dynamic Fee-to-Cost Gatekeeper. Before your AI executes any rebalance or pool switch, have it run a simple math check: Net_Profit = Unclaimed_Fees - (Estimated_Gas + Priority_Fee + Swap_Slippage). If Net_Profit is negative or below your tier's threshold, the AI must hold or simply widen its patience window.
• Single-Position Concentration at Low Balances. When your balance is under $500, force the AI to deploy 100% of active LP capital into one single pool. Splitting $50 across two or three pools doubles or triples your Solana account rent deposits (~0.05 SOL per position) and fragments your fee yield.
• Auto-Compound vs. Profit Skimming. Program the AI to auto-compound earned fees back into the LP position while in the Micro and Growth tiers to accelerate balance scaling. Once it crosses into the Scale tier, have it skim 20% to 50% of harvested fees into safe USDC or SOL reserves.
• Volatility-Adjusted Range Sizing. Feed your AI a simple volatility metric (such as Average True Range or standard deviation of hourly returns). When volatility spikes, the AI automatically widens the bin range to avoid getting immediately pushed out of range and incurring impermanent loss.

Recommended Architecture Flow

1. Balance Check Module. Reads current wallet SOL + USDC + active LP value.
2. Tier Selector. Loads the matching configuration rules (Micro, Growth, or Scale).
3. Pool Scanner. Queries Meteora DLMM or Orca APIs for pools matching the tier's minimum liquidity, volume-to-TVL ratio, and fee tier.
4. Execution & Profit Guard. Opens the position, monitors price boundaries, and blocks any rebalance that fails the Net_Profit check.
