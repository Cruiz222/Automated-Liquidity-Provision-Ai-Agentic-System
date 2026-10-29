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

## Added in milestone 5

* **Two-factor authentication**: RFC 6238 TOTP (checked against the RFC's test vectors), each code usable once, eight
  one-time recovery codes stored only as hashes, required at login and for withdrawals, with failures counted toward
  lockout. Disabling needs the password and a code.
* **Email verification and password reset**: 8-digit codes valid 15 minutes, five guesses, hashed at rest, issuance rate
  limited; reset codes go only to verified addresses, the request response never reveals whether an account exists, and a
  reset signs out every session (2FA stays on). An unverified address cannot block someone else from verifying it.
* **KYC tiers** drive withdrawal limits. The BVN is never stored (last four digits plus a keyed hash to stop reuse).
  Outside simulation nothing is auto-approved.
* **Payout destinations**: bank codes are checked against the account number (NUBAN), saved payout accounts are private
  per user, and an unconfirmed bank is never guessed (see `docs/PAYOUTS.md`).
* **Deployment**: public host and HTTPS origin handling, Secure cookies, HSTS, and a trusted-proxy mode that only believes
  a proxy on this machine; the database file is created owner-only (0600).

## Added in milestone 6

* **Transaction PIN** (replaces authenticator-based step-up and recovery codes). Six digits, must be set before any
  transaction, entered to confirm every action that moves or commits funds (deposits, conversions, positions, withdrawals,
  adding or removing payout accounts, accepting live terms). Weak PINs (repeats, sequences, common patterns) are refused;
  five wrong PINs lock PIN use for 15 minutes. Setting the first PIN needs the account password; changing it needs the old
  PIN; a forgotten PIN is reset with an emailed code plus the password.
* The PIN is hashed with PBKDF2 and a **server-side secret kept in a separate owner-only file** (`secret.key`), because
  a 6-digit PIN can be brute-forced offline from a leaked database alone. Back that file up separately; if it is lost,
  customers reset their PINs by email. A replay of an already-recorded operation needs no new PIN (it changes nothing).
* **Recovery codes are gone.** Account recovery is the email password reset, which also turns off the optional
  authenticator app (a lost phone cannot lock anyone out). The authenticator remains only as an optional login factor.
* **Outbound data fetches** are limited to an allowlist of public data hosts over HTTPS, refuse redirects, cap the response
  size, and fail closed: a source that is down or malformed yields no data, never guessed data.
* **Live mode** is off by default, fails closed, is gated per customer, can be paused by the operator, and isolates its books.
  Webhooks are HMAC-signed with replay protection and are reachable without a session but are useless without a valid signature.

## Residual risks and things not built

* **TLS is the proxy's job.** The server speaks HTTP on loopback only; see `docs/DEPLOYMENT.md` for a TLS proxy setup.
* Rate-limit counters live in memory and reset on restart. Per-username lockout lets an attacker lock a known
  account out for 15 minutes (a deliberate trade-off against password guessing).
* 2FA secrets and email addresses sit unencrypted in the database file (owner-only permissions); production needs
  encryption at rest with managed keys. No SMS or hardware-key 2FA, no admin or support tooling, no account recovery for
  a user who loses both their device and recovery codes.
* KYC is simulated: no real BVN or ID verification provider is connected, and limits are placeholders.
* The name enquiry is simulated and the bank list is a verified-subset starter; both must come from a payout provider.
  When the customer does not give a bank, the NUBAN check cannot reject a mistyped number; only the name enquiry can.
* Single process on SQLite; no per-connection concurrency cap beyond timeouts.
* The simulated FX rate and pool feed are placeholders. A real rate or pool provider needs source verification,
  signing or integrity checks, and chain/token identity validation.
* Destination checks are format-only (no ownership or checksum verification).
* Nigerian regulatory requirements for handling customer NGN and crypto, and custody design, are outside this code and
  need proper legal advice before any real funds.
