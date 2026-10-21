# Offline quote-width sensitivity

`app.quote_study` tests whether quote distance explains missing simulated fills. It verifies a frozen study and its source hashes, copies only development datasets, and varies `half_spread_bps` from one explicitly named reference scenario. The reference itself is always included. Other scenarios in the input are not included or combined with the width grid.

Cash, order size, inventory limits, inventory skew, fees, latency, participation, and queue assumptions stay fixed. Each dataset starts with a separate account. Validation and test datasets are verified for integrity but excluded from evaluation. This command never changes the paper runner, starts a feed, submits orders, or selects a winner.

For the existing qualified capture:

```bash
python3 -m app.quote_study data/workflow-b6568a5d02/study.lock.json
```

The default illustrative grid is 0.01, 0.1, 1, and 12 basis points per side. To specify a grid, append `--half-spreads-bps 0.1 1 12`. Use `--reference NAME` for a differently named input scenario. The grid accepts 1–16 unique finite nonnegative widths. These are sensitivity inputs, not recommended trading settings.

Each run creates a fresh `data/quote-study-*` workspace containing the base manifest, derived `study.lock.json`, and `summary.json`. The registry stores the reproducible experiment and full report (including execution diagnostics and the baseline without inventory skew). `--workspace PATH` requires a new directory; existing artifacts are never overwritten. Defaults use the existing `data/experiments.sqlite3` registry. A failed run may leave artifacts for inspection.

## First captured-data result

On October 21, 2026, the qualified development segment from `data/paper-b9ea9f520a/paper.sqlite3` was replayed via the frozen workflow above. It covers 914.1 seconds, with 5,254 normalized events and 1,376 trades. The account uses 0.001 BTC orders, a 0.01 BTC inventory limit, 2 bps fees, 100 ms latency, 10% participation, and zero assumed queue ahead.

| Half spread (bps) | Agent simulated fills | Net P&L (USDT) | Fees (USDT) |
| --- | ---: | ---: | ---: |
| 12 (reference) | 0 | 0 | 0 |
| 0.01 | 88 | -0.421826 | 0.287918 |
| 0.1 | 87 | -0.434914 | 0.277888 |
| 1 | 41 | -0.286240 | 0.175090 |

Experiment: `07be4cdb7805455eb4890738c1eff98d`. Local artifacts are in `data/quote-distance-first/summary.json` and its `experiments/` subdirectory. These data artifacts are ignored by Git.

Narrower quotes generate fills in this replay, but all tested non-reference widths lose money after fees. Net P&L marks remaining inventory at the final midpoint; the full report also provides liquidation value after fees. This is a single inspected development segment, not independent validation. Replay does not calibrate actual exchange queue position, tick/lot constraints, or real fills. The live default remains 12 bps.
