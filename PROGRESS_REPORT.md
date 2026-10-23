# Project progress report — 2026-10-23

## Executive summary

The repository has a substantial research and simulation toolkit, but the product path currently does not match the intended product. The existing dashboard and core engine are centered on synthetic prices, bid/ask quotes, fills, inventory skew and OKX paper trading. That is market-making/order-book research, not DEX liquidity provision. The wallet/DeFi modules are a useful early foundation, but are not connected to the main server/dashboard and are simulation-only.

## What exists already

- Persistent simulation journal, event replay and pause/resume controls.
- Synthetic quote/fill engine, execution assumptions and CSV backtesting.
- OKX public market-data capture, paper execution, feed diagnostics and research/evaluation workflows.
- SQLite wallet ledger with immutable entries, NGN/USDT balances, simulated deposits and idempotency keys.
- Short-lived NGN↔USDT conversion quotes with fee calculation, expiry, and idempotent execution.
- Simulated LP-position open/mark/close lifecycle with caller-supplied value and accrued fees.
- Tests for conversion and simulated LP position behavior.

## Milestone 3 changes in this patch

- `WalletLedger.withdraw(...)`: simulated withdrawal debits user balance and credits clearing; rejects insufficient balance; idempotent retry returns the original result; conflicting key reuse fails.
- `app/dex_pool_advisor.py`: validates pool inputs, applies hard risk gates, calculates transparent heuristic metrics, ranks eligible pools and recommends a bounded allocation without executing anything.
- New tests: `tests/test_wallet_withdrawal.py` and `tests/test_dex_pool_advisor.py`.
- README now clearly establishes DEX LP as the product direction and classifies the existing market-making tools as legacy research.

## Verification

The following focused suite passed in the build workspace:

```text
python3 -m unittest tests.test_wallet_withdrawal tests.test_dex_pool_advisor tests.test_defi_milestone -v
Ran 24 tests — OK
```

The full repository suite was attempted but did not finish within the available command timeout, so this patch does not claim a full-suite pass. Run the full suite on your machine before committing.

## Important design caveats found

- `app.server` and `app/dashboard.html` still serve the old quote/fill simulation; the new wallet and pool-advisor services are not exposed through authenticated product API routes.
- Conversion pricing currently defaults to a caller-configured NGN/USDT rate. It is not a live executable price and does not have quote-source provenance.
- LP position valuation and fees are caller-supplied; this is not an AMM position model and does not calculate impermanent loss.
- The ledger is a useful simulated ledger but is not yet a production double-entry accounting and reconciliation system.
- The pool advisor uses explicit heuristics, not a trained model. Its inputs are only as good as the provider that supplies them.
- The ZIP contains local research data and SQLite journals from the uploaded project. Do not commit runtime databases or secrets; `.gitignore` already excludes `data/` and `.env`.

## Next milestones

1. **M4 — DEX pool data contract/provider:** normalized pool schema, provider timestamp/source metadata, freshness checks, token/pool allowlists, deterministic fixtures and tests. Start with read-only public pool data.
2. **M5 — LP economics:** AMM-specific position accounting, pool fee accrual, token quantities, price impact, slippage and impermanent-loss/divergence-loss scenarios; compare net outcomes to holding tokens.
3. **M6 — wallet/API boundary:** authenticated user identity, account-scoped routes, balances/history/quotes/positions, withdrawal destination validation, rate limits and audit events. Keep simulated mode only.
4. **M7 — DEX product dashboard:** replace bid/ask/inventory UI with wallet balances, conversion preview, pool candidates, allocation proposal, LP positions, earned fees, risk and activity history.
5. **M8 — security and real integrations:** only after threat modeling, accounting review, legal/compliance review, contract/chain adapter review, wallet signing design, transaction simulation and explicit user approval. No real funds before these gates.

## Apply and verify

See `MILESTONE_3_APPLY.md`. Apply the patch, run the focused tests and full test suite, inspect `git diff`, then commit/push only after review. Do not deploy this milestone as a real-money service.
