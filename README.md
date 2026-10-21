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

This is a rule-based agent baseline. It does not yet use an AI model or connect to an exchange. The simplified fill model does not model queue position, latency, partial fills, or actual market liquidity. Simulated P&L is not evidence of trading profitability.

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

## Next implementation stages

1. Complete: persist market events, quotes, fills, and agent decisions for replay and audit.
2. Add recorded market-data replay and execution assumptions before evaluating strategies.
3. Add exchange market-data adapters and a paper execution interface.
4. Introduce an AI research agent with structured tools and bounded configuration proposals.
5. Add account isolation, authentication, monitoring, and deployment infrastructure.

Exchange trading requires a separate implementation and explicit operational configuration. This prototype has no credentials or live order route.
