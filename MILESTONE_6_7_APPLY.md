# Milestones 6–7 apply notes

Adds a local development API and account-scoped service layer for simulated NGN/USDT wallets and DEX LP-position records.

## Apply
From repository root:

```bash
unzip -o dex_liquidity_milestones_6_7.zip -d .
python3 -m unittest tests.test_product_service tests.test_product_api_server -v
python3 -m unittest discover -s tests -v
git diff --check
```

## Run locally

```bash
python3 -m app.product_api_server
```

Default bind: `127.0.0.1:8010`. Optional environment variables: `DEX_API_HOST` (loopback only), `DEX_API_PORT`, `DEX_API_DB`.

Endpoints:
- `GET /health`
- `POST /auth/register` with `username`, `password` (minimum 12 characters)
- `POST /auth/login` with `username`, `password`
- `POST /auth/logout` (Bearer token)
- `GET /wallet/balances`
- `GET /wallet/transactions?limit=50`
- `POST /wallet/deposits/simulated` and `/wallet/withdrawals/simulated` with `currency`, `amount_minor`, and `Idempotency-Key` header
- `GET /lp/positions`, `GET /lp/positions/{id}`, `POST /lp/positions`, `DELETE /lp/positions/{id}`

NGN amounts are in kobo; USDT amounts are in micro-USDT. All deposits and withdrawals are simulations; positions are records, not on-chain LP positions. This development server is not production-ready: accounts/sessions are in-memory, and it lacks TLS, rate limiting, durable authentication, email recovery, and production hardening. Do not expose it publicly or use real funds.
