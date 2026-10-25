# Security notes

This is a **simulation** that runs on loopback only. These controls exist so the patterns are right before any real
integration; they are not a substitute for a security review.

## Issues found in the previous build and how each is closed

| Issue | Fix |
|---|---|
| After a restart, anyone could register an existing username and see that user's wallet (users were in memory, balances persisted) | Users, sessions and audit are persisted in the same database as the ledger. Duplicate usernames are refused. |
| System ledger accounts (`clearing`, `liquidity-clearing`, `fees`, `withdrawal-hold`) could be registered as usernames, giving access to system balances | Ledger accounts are opaque ids (`u_<32 hex>`), never usernames. System names are also reserved at the ledger and username layers. |
| Liquidity positions never touched the wallet (a $1,000,000 position opened with no funds) | Positions debit and credit USDT through the ledger. Customers cannot supply valuations; the platform marks positions with a bounded simulation model. |
| Any user who learned a quote id could execute it, and any user could close another's position or cancel another's withdrawal | Quote execution, position close and withdrawal actions require the owning account; other users get "not found". |
| Idempotency keys were global, so users could collide or probe each other | Keys are namespaced per user and operation kind. Replays return the original result; a changed request under the same key is a conflict. |
| Advisor treated missing risk data as zero risk and trusted provider APR | The selector excludes pools with missing data, stale snapshots, or unaffordable gas, and caps fees by modelled fee tier and volume. |
| No ledger invariants | Every operation must net to zero per currency; customer balances cannot go negative; `reconcile()` verifies both. |
| Unlimited simulated deposits, no request limits | Per-request and per-day caps, a switch to disable simulation endpoints, per-IP and per-user rate limits. |

## Controls in place

* **Passwords**: PBKDF2-HMAC-SHA256 (600,000 iterations by default, parameters stored per hash and upgraded on login),
  12 to 128 characters, common/repetitive/username-containing passwords rejected.
* **Login**: failures limited per username and per address (5 and 30 per 15 minutes); unknown and known users take the
  same path and return the same error, so lockout does not reveal which usernames exist.
* **Sessions**: 256-bit random tokens, only the SHA-256 is stored; HttpOnly, SameSite=Strict cookie (Secure when
  configured); 8-hour absolute and 1-hour idle expiry; at most 5 per user; revoked on logout and on password change.
* **CSRF and cross-site**: per-session CSRF token required on every authenticated write; `Origin` and `Sec-Fetch-Site`
  checked; JSON-only bodies; no CORS headers; no bearer tokens.
* **DNS rebinding**: `Host` must be a loopback name for the server's port.
* **Input**: exact field whitelists and types (booleans and floats never accepted as integers), 16 KiB body cap,
  bounded query parameters, destination format checks, no chunked bodies.
* **Output**: strict CSP (no inline script or style), nosniff, no-store, frame denial, COOP/CORP. The dashboard builds
  the page with `textContent` only. Static files come from a fixed allowlist. Errors are generic; 500s carry only a
  reference id.
* **Operations**: socket timeouts, one request per connection, no access log of URLs or bodies, append-only audit table
  (database triggers block edits and deletes), money moves only through the ledger.

## Residual risks and things not built

* **No TLS.** Run behind a TLS-terminating proxy and set `DEX_SECURE_COOKIES=1` before any non-local use; the server
  deliberately refuses non-loopback binds. `X-Forwarded-For` is ignored, so per-IP limits see the proxy's address.
* Rate-limit counters live in memory and reset on restart. Per-username lockout lets an attacker lock a known
  account out for 15 minutes (a deliberate trade-off against password guessing).
* No email verification, password reset, two-factor authentication, KYC or admin tooling.
* Single process on SQLite; no per-connection concurrency cap beyond timeouts.
* The simulated FX rate and pool feed are placeholders. A real rate or pool provider needs source verification,
  signing or integrity checks, and chain/token identity validation.
* Destination checks are format-only (no ownership or checksum verification).
* Nigerian regulatory requirements for handling customer NGN and crypto, and custody design, are outside this code and
  need proper legal advice before any real funds.
