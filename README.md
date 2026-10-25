# Liquidity Lab: simulated NGN/USDT wallet with DEX liquidity-provision screening

A Python platform where a customer registers, holds **NGN** and **USDT** wallets, converts between them with
priced quotes, withdraws, and places USDT into DEX liquidity pools chosen by a transparent screening engine.
It includes a hardened JSON API and a browser dashboard.

> **Simulation only.** Nothing connects to a bank, payment provider, exchange, wallet or blockchain. The FX rate
> is a fixed placeholder and the pool data is synthetic example data, not market data. Do not use it with real
> money. The server refuses to bind to anything but a loopback address.

## Run it

Python 3.10+ and nothing else (standard library only).

```bash
python3 -m app                     # then open http://127.0.0.1:8010
python3 -m app.demo                # command-line walkthrough of the same flow
python3 -m unittest discover -s tests -t .
```

Optional environment: `DEX_PORT`, `DEX_DB` (default `data/platform.sqlite3`), `DEX_POOLS` (snapshot JSON),
`DEX_SIM_TIME_SCALE`, `DEX_SECURE_COOKIES=1` (only behind HTTPS).

## What the dashboard does

Registration and login; NGN and USDT balances and portfolio value; simulated deposits; withdrawals with fee,
limits, cancellation and a simulated payout provider; conversion quotes with rate, fee, expiry countdown and
execution; pool screening with risk, fee-vs-impermanent-loss and a what-if calculator; a suggested allocation you
can deploy; position creation, listing and closing; transaction history; and a security page (account activity,
password change, sign out everywhere). A permanent banner states that everything is simulated.

## Layout

```
app/
  ledger/      double-entry NGN/USDT ledger: deposits, withdrawals, holds, fees, reconcile()
  fx/          rate sources and service (bid/ask, staleness, deviation guard) and conversion quotes
  dex/         pool snapshots, store and provider, IL and AMM math, pool selector and allocation
  portfolio/   ledger-backed liquidity positions, valuation, allocation deployment
  accounts/    users, sessions, rate limiting, audit trail
  service.py   account-scoped business rules (the only layer the API calls)
  api/         hardened HTTP server and the dashboard (static/)
  config.py    settings;  demo.py  CLI walkthrough;  __main__.py  server entry point
examples/      synthetic pool snapshot
docs/          ARCHITECTURE.md, SECURITY.md
tests/         unit, service and HTTP security tests
```

See `docs/ARCHITECTURE.md` for design decisions and what is not built yet, and `docs/SECURITY.md` for the threat
model, controls and residual risks.
