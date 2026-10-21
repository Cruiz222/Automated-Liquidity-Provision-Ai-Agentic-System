# Automated Liquidity Provision AI Agentic System

Liquidity Lab is a local prototype for researching an automated liquidity provision platform. It includes a synthetic market, an inventory-aware quoting agent, simulated execution, risk controls, and a browser dashboard. Python 3.10+; no third-party dependencies.

## Run

```bash
python3 -m app.server
```

Open http://127.0.0.1:8000. The agent advances every second. Pause and resume it from the dashboard; restarting the process resets the simulation.

```bash
python3 -m unittest discover -s tests -v
```

## Current behavior

The agent places a bid and, when inventory is available, an ask around a synthetic market price. Inventory shifts its quotes to reduce additional accumulation. Orders fill when the next synthetic price crosses their quote. Cash includes a configurable fee on every fill; equity marks inventory at the current market price. The account is long-only and cannot buy beyond its cash or inventory limits. State is stored in memory, retaining 200 price observations and 100 fills.

This is a rule-based agent baseline. It does not yet use an AI model or connect to an exchange. The simplified fill model does not model queue position, latency, partial fills, or actual market liquidity. Simulated P&L is not evidence of trading profitability.

## Next implementation stages

1. Persist market events, quotes, fills, and agent decisions for replay and audit.
2. Add recorded market-data replay and execution assumptions before evaluating strategies.
3. Add exchange market-data adapters and a paper execution interface.
4. Introduce an AI research agent with structured tools and bounded configuration proposals.
5. Add account isolation, authentication, monitoring, and deployment infrastructure.

Exchange trading requires a separate implementation and explicit operational configuration. This prototype has no credentials or live order route.
