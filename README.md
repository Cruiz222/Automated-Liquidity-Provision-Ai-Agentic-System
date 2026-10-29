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
