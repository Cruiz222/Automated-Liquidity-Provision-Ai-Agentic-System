# Frozen execution-sensitivity studies

The offline workflow evaluates predefined execution scenarios across separate capture CSVs. It compares the inventory-aware agent with the fixed-spread, zero-skew baseline. Each dataset starts a fresh account: quotes, inventory, cash, and fills never cross capture boundaries. The workflow produces comparisons, not a selected strategy or deployment decision.

## Run the synthetic example

```bash
python3 -m app.evaluate freeze examples/evaluation_plan.json --output data/study.lock.json
python3 -m app.evaluate run data/study.lock.json --output data/study-report.json
```

Both output paths must be new. The supplied plan uses handcrafted synthetic data as a development-only pipeline check, not a historical performance benchmark.

## Plan and provenance

A plan declares version `1`, one instrument, shared `config`, `datasets`, and named `scenarios`. Dataset paths are resolved relative to the plan file. Each dataset has a unique name, a CSV path following the market replay contract, and a role: `development`, `validation`, or `test`. Use a consistent epoch time base when comparing real captures. The tool cannot infer the instrument or unit conventions from CSV prices; the declaration must match every input.

A scenario sets an `execution` object with `latency_ms`, `participation`, and `queue_ahead`, and may override `fee_bps`. Omitted settings use the existing model defaults. Quoting parameters come from the shared configuration and remain fixed across scenarios. Unknown plan fields fail rather than silently ignoring misspelled settings.

Freeze validates every CSV and records its exact bytes' SHA-256, row count, time range and resolved absolute path. It expands each scenario into its effective configuration and execution assumptions. A canonical manifest digest identifies the complete frozen study. Duplicate content and overlapping capture time ranges are rejected, including overlaps within the same role. This prevents counting the same period twice; it does not establish statistical independence.

Run validates the frozen manifest digest and rechecks all source bytes before evaluating anything. Changed inputs or settings require an explicit new freeze. Keep the data files at the frozen paths or freeze a new plan after moving them. No input data is downloaded or modified by these commands. A manifest hash detects ordinary changes; it is not a signed authenticity mechanism.

## Roles and results

Choose scenarios using development data. Use separate later captures for validation and a final evaluation with test data. The tool preserves role labels and prevents overlap, but cannot establish that a human has never viewed or reused a validation/test set. Any already-inspected capture must be described as such.

Reports retain the frozen manifest, interpreter and model version, source hashes, and per-dataset/per-scenario agent and baseline metrics and execution outcomes. Per-role summaries report equal-weight mean, minimum and maximum segment P&L, worst segment drawdown, and total fill count. These summarize independent account resets; they are not one compounded portfolio or continuous equity curve. Short or zero-duration samples and few or zero fills remain inconclusive.

CSV evaluation uses event-time decisions. It therefore evaluates the recorded market stream under offline assumptions and does not reproduce live poll receipt delays, carried inventory, or pause actions. Use `app.paper_report` for the original paper account audit. Queue and participation settings are sensitivity assumptions, not exchange-calibrated estimates; actual execution calibration still needs credible fill observations.

## Completed local demonstration

Two already-inspected, nonoverlapping segments from `data/paper-6d3d79ee38/validated-export` were frozen under separate development and validation labels, then evaluated with the paper defaults and a slower, lower-participation scenario with queue ahead and higher fees. All four agent evaluations had zero fills, so the experiment provides no basis to select execution settings or claim an advantage. The local report is `data/captured-study-report.json`, with frozen manifest `data/captured-study.lock.json`. These are local research artifacts excluded from Git.
