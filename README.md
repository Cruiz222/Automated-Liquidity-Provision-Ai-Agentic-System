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

This is a rule-based agent baseline. The quoting strategy is rule-based; an optional research-only model adapter can propose offline stress studies. A separate paper runner connects to public exchange market data with simulated orders. The simplified fill model does not model queue position, latency, partial fills, or actual market liquidity. Simulated P&L is not evidence of trading profitability.

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

## Longer capture and quality checks

```bash
python3 -m app.paper run --dns-mode doh --duration-seconds 1800
```

Timed runs stop automatically and save `capture-review.json` beside their journal. The review checks each continuity segment against explicit duration, trade-count, event-spacing, feed-health and alignment criteria, with accounting verification. See [collection and review criteria](COLLECTION.md). Passing data checks does not establish strategy profitability or calibrate real exchange fills.

## Research proposals

The research proposer validates completed capture evidence and frozen datasets, then produces either a request for more data or a selection from fixed offline stress scenarios. An offline deterministic mode needs no credentials; an optional OpenAI adapter generates schema-constrained proposals. A separate materialize command creates a development-only study, preserving live settings and account limits. See [research workflow and API setup](RESEARCH_AGENT.md).

## Registered experiments

```bash
python3 -m app.experiments run data/study.lock.json
python3 -m app.experiments list
```

Offline studies now have persistent IDs, run statuses, input/implementation fingerprints and hashed report artifacts. Matching completed studies can be reused after verification. The dashboard includes a read-only experiment list and details. See [registry operations and interruption handling](EXPERIMENTS.md).

## End-to-end offline workflow

```bash
python3 -m app.pipeline data/paper-first/paper.sqlite3
```

The workflow reviews a completed capture, stops when more data is needed, or prepares qualifying development segments, freezes a study, generates a bounded offline proposal and registers its evaluation. Each phase is checkpointed in a fresh workspace. See [pipeline commands, outputs and exit codes](PIPELINE.md).

## Operations overview

The dashboard now combines current feed health with saved capture reviews, workflow checkpoints and experiment attempts. Its read-only Operations panel suggests a concrete next action without starting work automatically. The same saved-artifact summary is available with `python3 -m app.operations`. See [discovery, freshness and next-action rules](OPERATIONS.md).

## Streaming market data

An optional WebSocket transport now processes public book and trade updates through a bounded reorder buffer, removing the REST model's dependence on a new trade to advance book events. It uses the same journal and offline tools, with reconnect cancellation, local snapshot resynchronization and unchanged numeric quality limits. Capture review measures alignment only when a processed snapshot establishes the segment clock. See [setup, live command and timing evidence](WEBSOCKET_FEED.md).

## Next implementation stages

1. Complete: persist market events, quotes, fills, and agent decisions for replay and audit.
2. Complete: CSV market-data replay, explicit execution assumptions, and baseline performance reports. Historical data acquisition and execution calibration remain open.
3. Complete: read-only OKX spot REST adapter, segmented capture exports, paper execution, and feed-health monitoring. Public connectivity has been verified with opt-in DoH in this environment; execution calibration remains open.
4. Complete: bounded research proposals, deterministic offline mode, and an optional structured-output model adapter. Live model generation and evaluation of proposal quality remain open.
5. Add account isolation, authentication, monitoring, and deployment infrastructure.

Exchange trading requires a separate implementation and explicit operational configuration. This prototype has no credentials or live order route. Public market-data access depends on network reachability and exchange availability.

Offline quote-width sensitivity is available with `python3 -m app.quote_study data/workflow-b6568a5d02/study.lock.json` when that local workflow exists. See [QUOTE_STUDY.md](QUOTE_STUDY.md) for development-only input rules, recorded results, and execution limitations.


1. Discover and evaluate markets

The system continuously evaluates eligible coins and trading pairs using market activity, spreads, liquidity, volatility, trading costs, and risk.




2. Select the best opportunity

It ranks candidate markets based on their expected risk-adjusted net returns from providing liquidity, rather than simply choosing the coin with the highest trading volume or most volatile price.




3. Allocate available capital

It decides how much capital to commit to each selected market, while reserving funds for fees, losses, inventory changes, and other risk limits.




4. Provide and manage liquidity

It places appropriate buy and sell quotes—or, if targeting a decentralized exchange, manages liquidity positions in a pool—and adjusts or removes them as market conditions change.




5. Monitor, learn, and protect capital

It tracks performance after fees and inventory losses, detects deteriorating conditions, updates its decisions using evidence, and stops or reduces exposure when risk limits are reached.


The product we should build
User account and wallet

Account, identity verification, NGN deposits and balances

Funding and conversion

NGN → USDT, with verified rates, fees and transaction records

AI pool selection and allocation

Evaluate pools, forecast net fee income, assess risk and set capital limits

DeFi liquidity engine

Convert USDT into required token pairs and provide liquidity to selected pools

Portfolio monitoring and accounting

Track positions, earned fees, token values, losses, withdrawals and each user's share


How we should implement this in our project
I recommend making IL mitigation part of the AI's core decision process, with three pool categories:
Pool category	Initial policy
Stablecoin–stablecoin	Evaluate first as a lower-relative-price-risk candidate, while checking depeg and contract risks
Correlated volatile assets	Evaluate for moderate divergence risk and fee potential
Uncorrelated volatile assets	Apply stricter risk limits and require stronger evidence that fees compensate for divergence risk