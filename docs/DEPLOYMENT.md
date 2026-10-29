# Running behind TLS

The server only binds to a loopback address and speaks plain HTTP. To reach it from elsewhere, put a TLS-terminating
reverse proxy **on the same machine** in front of it. This is still a simulation: do not expose it with real money.

## Settings

| Variable | Meaning |
|---|---|
| `DEX_PUBLIC_HOST` | The public host name, e.g. `lab.example.com` (added to the allowed `Host` and `Origin`) |
| `DEX_PUBLIC_HTTPS=1` | Public site is HTTPS: Secure cookies, HSTS, https-only origin |
| `DEX_TRUST_PROXY=1` | Use the last `X-Forwarded-For` entry from the local proxy as the client address (per-IP limits and audit) |
| `DEX_SMTP_HOST`, `DEX_SMTP_PORT`, `DEX_SMTP_FROM`, `DEX_SMTP_USER`, `DEX_SMTP_PASSWORD` | Send real email over STARTTLS; unset means codes go to the console and `data/outbox.log` |
| `DEX_BANKS` | Path to a replacement bank directory JSON |
| `DEX_DATA=live` | Use live DefiLlama pool data (default `fixture` = synthetic example) |
| `DEX_POOL_CHAINS`, `DEX_POOL_PROJECTS` | Narrow the live pool universe (comma-separated) |
| `DEX_RATE_SOURCES` | Real NGN/USDT reference sources: preset names (e.g. `coingecko-tether-ngn`) or `url\|json.path` |
| `DEX_LIVE=1`, `DEX_LIVE_RAILS=sandbox`, `DEX_WEBHOOK_SECRET`, `DEX_LIVE_PAUSED`, `DEX_LIVE_DB` | Live books (sandbox rails only); see `docs/LIVE_MODE.md` |
| `DEX_DB`, `DEX_PORT`, `DEX_POOLS`, `DEX_SIM_TIME_SCALE` | As in the README |

`X-Forwarded-For` is believed only when the connection comes from `127.0.0.1` or `::1`, and only the last entry is
used, so a client cannot choose its own address as long as the proxy appends the real one.

## Caddy (automatic HTTPS)

```
lab.example.com {
    reverse_proxy 127.0.0.1:8010
}
```

```bash
DEX_PUBLIC_HOST=lab.example.com DEX_PUBLIC_HTTPS=1 DEX_TRUST_PROXY=1 python3 -m app
```

## nginx

```
location / {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

## Before any real use

Back up `data/secret.key` separately from the database (it peppers PIN hashes). Encrypt secrets at rest (authenticator
secrets and email addresses are stored in the database file, protected only by its owner-only permissions), move to a production database, add monitoring and backups, and complete the compliance work
listed in `docs/SECURITY.md`.
