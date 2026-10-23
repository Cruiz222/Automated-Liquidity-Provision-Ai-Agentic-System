# Milestone 3 — simulated withdrawals and DEX pool advisor

## Apply

Extract this patch ZIP into the repository root and choose **merge/overwrite** for matching paths. It contains the updated `README.md`, the modified `app/wallet_ledger.py`, new `app/dex_pool_advisor.py`, two new test files, and the product/progress docs. It does not contain your runtime `data/` directory.

From your repository root:

```bash
python3 -m unittest tests.test_wallet_withdrawal tests.test_dex_pool_advisor tests.test_defi_milestone -v
python3 -m unittest discover -s tests -v
 git diff --check
 git status --short
 git diff -- README.md app/wallet_ledger.py
```

If your virtual environment is `.venv`, use `.venv/bin/python` instead of `python3`.

## New API: simulated withdrawal

```python
from app.wallet_ledger import WalletLedger
ledger = WalletLedger("data/wallet_ledger.sqlite3")
ledger.deposit("user-1", "NGN", 150_000, "deposit-001")
result = ledger.withdraw("user-1", "NGN", 50_000, "withdrawal-001")
print(result, ledger.balances("user-1"))
ledger.close()
```

Amounts are minor units: NGN uses kobo, USDT uses micro-USDT. A repeated call with the same idempotency key and same request returns the original result; reusing a key for a different request raises `IdempotencyConflict`. This is a simulated ledger entry, not a payout.

## New API: pool evaluation and allocation

```python
from app.dex_pool_advisor import PoolCandidate, evaluate_pool, recommend_allocation
pool = PoolCandidate.from_dict({
    "pool_id": "example-usdc-usdt", "chain": "base", "dex": "ExampleDEX",
    "token0": "USDC", "token1": "USDT", "tvl_usd": "1000000",
    "volume_24h_usd": "500000", "fee_apr_pct": "12", "volatility_pct": "2",
    "depeg_risk_pct": "3", "contract_risk_pct": "5",
    "correlation_risk_pct": "10", "gas_cost_usd": "1",
    "category": "stable_stable"
})
print(evaluate_pool(pool))
print(recommend_allocation([pool], capital_usd="1000", max_pool_fraction="0.25"))
```

Inputs are illustrative only. The score is a transparent heuristic, not a prediction. Allocation output is a suggestion; no wallet movement or on-chain transaction is performed.
