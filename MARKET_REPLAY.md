# Recorded market-data replay

The offline runner imports one instrument's book snapshots and optional trades, runs the inventory-aware agent and a fixed-spread baseline, and produces an auditable JSON report. It uses no network access and does not change simulation journals. The supplied sample is handcrafted synthetic data for exercising the pipeline; it is not historical exchange data or performance evidence.

## Run

```bash
python3 -m app.backtest examples/market_sample.csv
python3 -m app.backtest examples/market_sample.csv --output data/sample-report.json
```

Output files must be new: existing files are never overwritten. Reports include the exact source bytes' SHA-256, interpreter and model versions, configuration, execution assumptions, each strategy's quote decisions, fills, and equity curve. Replay of the same input and settings is deterministic.

The default execution settings are 100 ms latency, 10% trade participation, zero assumed queue ahead, and a 2 bps fee on every fill. These are illustrative settings, not calibrated exchange estimates. Change them explicitly:

```bash
python3 -m app.backtest examples/market_sample.csv --latency-ms 200 --participation 0.05 --queue-ahead 1 --fee-bps 3 --order-size 1 --max-inventory 10 --half-spread-bps 12 --inventory-skew-bps 20
```

The baseline has zero inventory skew, with the same spread, cash, order size, inventory limit, fees, and execution model as the agent. Both start with 100,000 cash and no inventory. It is evaluated as a separate hypothetical account, so the two strategies do not compete for the same trade volume. The report also includes a cash benchmark with zero P&L.

## Input contract

CSV columns must appear in this order:

```csv
timestamp_ms,bid,ask,bid_size,ask_size,trade_price,trade_size,trade_side
0,99.5,100.5,20,20,,,
100,99.5,100.5,20,20,99.8,5,sell
```

- `timestamp_ms`: nonnegative integer milliseconds in one consistent time base. Epoch milliseconds or elapsed milliseconds both work. Timestamps must be nondecreasing; ties retain file order. At least two rows are required.
- `bid`, `ask`: finite positive best prices, with bid strictly below ask. Locked and crossed books are rejected.
- `bid_size`, `ask_size`: finite nonnegative displayed quantities in base units. These validate the book snapshot but do not reveal queue position at our limit price or determine passive fills.
- Trade fields: all blank, or all supplied. Price and size must be finite and positive. `trade_side` is the **aggressor** side: `sell` may hit our bid; `buy` may lift our ask. Each trade must appear exactly once in the source.

Each row represents a book snapshot immediately before its optional trade. An adapter must align source feeds to this convention; the importer cannot infer the ordering of exchange book and trade messages. Use one instrument, one quote currency, and consistent base-size units per file. There is no resampling, automatic sorting, or duplicate-trade detection. Malformed rows fail with their CSV line number.

## Event and execution order

1. Carry the previous event's inventory and midpoint through the elapsed interval for exposure accounting.
2. Activate a pending quote replacement if its latency has elapsed. The old pair remains active until arrival, then is cancelled atomically. A new bid at or above the current ask, or ask at or below the current bid, is rejected as marketable.
3. Match an optional trade against the appropriate active order if its price reaches our limit. Available flow is trade size times participation. Deduct assumed queue ahead, then fill the remaining order quantity up to that flow and current account limits. A fill uses our limit price and pays the configured fee.
4. Mark account equity at the current midpoint. If no replacement is pending, generate the next quotes. The last row submits no new quotes.

Even with zero latency, quotes generated from a row cannot fill against that row's trade. Old orders can partially fill across rows until a replacement arrives. Every replacement resets queue ahead. There is at most one replacement in flight. The strategy inherits the simulation's long-only rule: it submits asks only when a full order size is available and bids only when a full order fits cash and inventory limits. Stale active orders are clipped at execution to prevent overspending or selling unavailable inventory.

## Report interpretation

Metrics include net P&L after fees, return, absolute and percentage maximum drawdown from marked equity, final cash and inventory, maximum inventory, time-weighted inventory and inventory notional, fill count, filled quantity, and quantity fill rate. Fill rate divides filled base quantity by all submitted base quantity, including replaced or rejected orders. Exposure averages are `null` when elapsed duration is zero.

Midpoint equity includes unrealized inventory value. An additional final-bid liquidation estimate deducts the configured fee but does not account for depth or slippage. Queue ahead and participation are explicit approximations; this model does not reconstruct order books, exchange priority, tick/lot rules, hidden liquidity, market impact, or funding. Positive replay P&L cannot establish a tradable advantage. Next steps are exchange-specific data adapters, calibration of execution assumptions, and evaluation on untouched historical periods.

Both input and detailed reports currently reside in memory during a run. Large datasets will need streaming and incremental report storage.
