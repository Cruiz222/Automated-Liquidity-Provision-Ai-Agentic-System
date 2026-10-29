# Demo funds, live funds, and what "live" means today

## The two modes

Every customer has **two separate sets of books**: *demo* and *live*. Each has its own ledger database, conversion
quotes and positions, so a bug or a test in one cannot touch the other. Users, identity checks (KYC), payout accounts and
pool data are shared. The funds mode is chosen per request (`X-Funds-Mode: demo|live`, default demo); the dashboard's
**Demo funds / Live funds** switch sets it, and a permanent banner always says which books you are looking at.

| | Demo funds | Live funds |
|---|---|---|
| Pool data | Real (DefiLlama) with `DEX_DATA=live`, else synthetic example | Same |
| Positions | Paper-traded: valued from real prices, fees accrued in real time. Nothing is executed on-chain | Opened and closed through a chain executor |
| Deposits | Instant, capped, simulated | Rail gives deposit instructions; credited only by a **signed webhook** |
| Withdrawals | Simulated payout buttons | Sent to the payout rail; completed or failed by signed webhook |
| Conversion rate | Real reference rate if configured, else a fixed placeholder | Needs **two** agreeing real sources, otherwise blocked |

## What is built

* Separate live ledger, off by default (`DEX_LIVE=1`), with a startup integrity check.
* **Gates**: live is refused unless the server enabled it, is not paused (`DEX_LIVE_PAUSED=1` kill switch), has rails
  configured, the live ledger reconciles, and the customer has verified identity (tier 1+), a verified email, a PIN, and has
  accepted the live terms (recorded with a version). The dashboard lists every unmet item.
* **Adapter interfaces** for a payment rail (NGN deposits and payouts) and a chain executor (USDT sends and on-chain
  positions), with an HMAC-SHA256 signed-webhook scheme (timestamp tolerance, constant-time compare, event de-duplication).
* A failed rail or chain call never leaves funds stuck: a rejected payout releases the hold, a refused position is refunded.
* **Sandbox adapters** that touch nothing: `DEX_LIVE_RAILS=sandbox`. They let the whole live flow run end to end.

## What is NOT built (so live cannot move real money)

* A **real payment rail adapter** (bank or payment-provider integration) and a **real chain executor** (key custody,
  transaction signing, DEX contract calls). The settings refuse any `live_rails` value except `sandbox`. These need a
  licensed provider, credentials and security review, and cannot be tested here; shipping them untested would risk real funds.
* **Live position valuation from the chain.** Live positions currently use the same paper model as demo. A real executor must
  report actual position value and fees.
* **Live USDT deposits** (chain watcher), stuck-payout recovery jobs, reconciliation against provider statements, an admin
  console, and monitoring.
* **A hedge or liquidity source for NGN/USDT conversions.** In live mode the platform is the counterparty and carries
  the FX risk; the operator must fund and manage inventory.
* Licensing and compliance. Handling customer NGN and crypto is regulated in Nigeria; this code does not make an operator
  compliant, and the live terms shown in the app are placeholders to replace with approved text.

## Trying the sandbox

```bash
export DEX_LIVE=1 DEX_LIVE_RAILS=sandbox DEX_WEBHOOK_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(40))')"
python3 -m app
# in the dashboard: complete KYC, verify email, set a PIN, then Live funds > accept terms > switch
# request a live deposit, then play the bank with the signed-webhook tool:
python3 -m app.sandbox_webhook deposit --reference D... --amount 500000000
python3 -m app.sandbox_webhook payout --withdrawal-id "wd:..." --outcome completed
```

Live conversions also need two rate sources (`DEX_RATE_SOURCES`), for example the CoinGecko preset plus a second source
you trust.

## Before any real money

Implement and test real adapters with a licensed provider; pen-test the webhook endpoints; encrypt secrets at rest and
manage the PIN pepper and webhook secret in a secret store; add reconciliation against provider statements and alerting;
replace the terms with approved text; get legal advice; run on testnets or with tiny amounts first.
