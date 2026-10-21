# Automated Liquidity Provision AI Agentic System

Liquidity Lab is a local prototype for researching an automated liquidity provision platform. It includes a synthetic market, an inventory-aware quoting agent, simulated execution, risk controls, and a browser dashboard. Python 3.10+; no third-party dependencies.

## Run

```bash
python3 -m app.server
```

Open http://127.0.0.1:8000. The agent advances every second. Pause and resume it from the dashboard; restarting the process recovers the saved simulation, including its pause state.

```bash
python3 -m unittest discover -s tests -v
```

## Current behavior

The agent places a bid and, when inventory is available, an ask around a synthetic market price. Inventory shifts its quotes to reduce additional accumulation. Orders fill when the next synthetic price crosses their quote. Cash includes a configurable fee on every fill; equity marks inventory at the current market price. The account is long-only and cannot buy beyond its cash or inventory limits. The dashboard retains 200 price observations and 100 fills; a SQLite journal retains all simulation events on disk.

This is a rule-based agent baseline. It does not yet use an AI model. A separate paper runner connects to public exchange market data with simulated orders. The simplified fill model does not model queue position, latency, partial fills, or actual market liquidity. Simulated P&L is not evidence of trading profitability.

## Persistence and replay

The server creates `data/simulation.sqlite3`, with the initial configuration, random seed, and ordered events. Every tick stores its submitted quotes, resulting market and account state, and fills. Pause and resume actions are also journaled. An event commits before the new state becomes visible. Storage failures stall advancement and appear on the dashboard; the worker retries. A competing writer is rejected and must restart to recover the latest journal.

On startup, the server verifies and replays every event, restoring the random generator sequence and account state. Invalid or modified events stop recovery. This verifies consistency, not cryptographic authenticity. Recovery time grows with journal size; checkpoints and migrations are future work. Keep the Python runtime version consistent for reproducible random generation. The event format is versioned; incompatible engine behavior requires an explicit migration or a new journal.

Verify a saved run without changing it:

```bash
python3 -m app.replay data/simulation.sqlite3
```

Start a separate simulation without replacing the existing run:

```bash
python3 -m app.server --journal data/another-run.sqlite3
```

Replay outputs the verified event count and reconstructed state as JSON. Journal files contain local research data and are excluded from Git.

## Recorded market-data replay

Run the offline evaluator against a validated single-instrument CSV:

```bash
python3 -m app.backtest examples/market_sample.csv --output data/sample-report.json
```

The supplied file is handcrafted synthetic data. Replace it with recorded data following the [CSV contract](MARKET_REPLAY.md). The runner models configurable latency, trade participation, queue ahead, partial fills, and fees. Its JSON report compares the inventory-aware agent with a zero-skew baseline and includes source provenance, decisions, fills, equity, drawdown, and inventory exposure. This offline model is separate from the dashboard simulation and its existing journal format. See [execution assumptions and report interpretation](MARKET_REPLAY.md).

## Live public feed and paper trading

Check connectivity first with `python3 -m app.network_check`. The default REST endpoint is `https://openapi.okx.com`; If system DNS fails and you have no sudo access, use `python3 -m app.network_check --dns-mode doh` and add `--dns-mode doh` to the paper runner; this changes only the application transport.

```bash
python3 -m app.paper run --run-dir data/paper-first --polls 30
```

Open http://127.0.0.1:8000. The separate OKX public REST runner captures BTC-USDT data, checks freshness and trade-window overlap, buffers book updates until the trade watermark reaches them, and simulates orders using the replay execution model. It records raw responses, decisions, fills, account state and feed faults in its own SQLite journal. The dashboard shows feed health and paper-account activity. No credentials are required. See [paper operation, capture exports, and feed limitations](PAPER_TRADING.md).

## Execution diagnostics

New paper runs record fill and non-fill outcomes and show their cumulative counts on the dashboard. Inspect a saved journal offline:

```bash
python3 -m app.paper_report data/paper-first/paper.sqlite3
```

The report checks account balances against fills and fees, separates recorded outcomes from historical inferences, and summarizes feed faults and missed quote distances. See [audit limitations](PAPER_TRADING.md#execution-diagnostics-and-account-audit). These diagnostics do not automatically tune strategy parameters.

## Frozen multi-capture evaluation

```bash
python3 -m app.evaluate freeze examples/evaluation_plan.json --output data/study.lock.json
python3 -m app.evaluate run data/study.lock.json --output data/study-report.json
```

Freeze hashes datasets and effective scenario settings. Run verifies them, evaluates each segment with a separate account, and reports agent/baseline results by development, validation, or test role. See [study plans and interpretation](EVALUATION.md). The synthetic example demonstrates the workflow; scenarios remain illustrative execution assumptions.

## Next implementation stages

1. Complete: persist market events, quotes, fills, and agent decisions for replay and audit.
2. Complete: CSV market-data replay, explicit execution assumptions, and baseline performance reports. Historical data acquisition and execution calibration remain open.
3. Complete: read-only OKX spot REST adapter, segmented capture exports, paper execution, and feed-health monitoring. Public connectivity has been verified with opt-in DoH in this environment; execution calibration remains open.
4. Introduce an AI research agent with structured tools and bounded configuration proposals.
5. Add account isolation, authentication, monitoring, and deployment infrastructure.

Exchange trading requires a separate implementation and explicit operational configuration. This prototype has no credentials or live order route. Public market-data access depends on network reachability and exchange availability.
