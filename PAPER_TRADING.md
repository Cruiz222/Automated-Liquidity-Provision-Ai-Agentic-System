# Public feed capture and paper trading

The runner captures OKX spot order-book snapshots and recent trades using public GET requests, then runs local simulated orders through the same incremental execution model as CSV replay. It needs Python 3.10+ and no extra dependencies, credentials, or exchange account. The default instrument is BTC-USDT; the adapter accepts USDT spot pair names. There is no authenticated client or order-submission route.

## Run and monitor

```bash
python3 -m app.paper run --run-dir data/paper-first --polls 30
```

Open http://127.0.0.1:8000 for feed status, account state, active quotes, recent quote decisions, simulated fills, and price history. The badge identifies live public data with paper orders. Pause cancels simulated orders while capture continues. Resume starts a new continuity segment from a fresh snapshot and ignores catch-up trades. Pause state does not persist across independent paper runs.

For a continuous capture, omit `--polls`. Stop with Ctrl+C. A run directory must be new; existing results are never overwritten. If port 8000 is in use by the synthetic dashboard, specify `--port 8001`. A bounded run exits with an error if no poll accepts fresh data. Capture without a web server:

```bash
python3 -m app.paper run --run-dir data/paper-headless --polls 30 --no-dashboard
```

Defaults: one-second poll interval, five-second freshness limit, 0.001 base-unit order size, 0.01 base-unit maximum inventory, 100,000 USDT simulated starting cash, 100 ms simulated latency, 10% participation, zero queue ahead, and 2 bps per-fill fees. These are illustrative settings, not calibrated OKX execution estimates or actual account fee rates. Parameters are available through `--help`.

## Feed checks and alignment

The default REST base URL is `https://openapi.okx.com`, which OKX [recommends for global REST access](https://www.okx.com/docs-v5/log_en/#2026-05-20). Select an applicable official regional endpoint with `--base-url`; endpoints are not switched automatically. The selected endpoint is recorded in run metadata.

The adapter uses `GET /api/v5/market/books?sz=1` followed by `GET /api/v5/market/trades?limit=500`. The [official API reference](https://app.okx.com/docs-v5/en) describes the snapshot timestamp, recent-trade window, trade IDs, and taker-side field. Spot sizes are interpreted as base units. No numeric adjacency of trade IDs is assumed.

The first successful poll establishes a book and latest trade anchor. Historical trades in that response are skipped. Later polls require the previous anchor to remain in the returned trade window; repeated IDs are ignored. Conflicting duplicate IDs in one response, malformed data, inconsistent ID/time ordering, regressing book timestamps, and future or stale snapshots are rejected.

Trades are replayed chronologically against the most recent captured book with a timestamp no later than the trade. The book is held constant between captured snapshots. A fresh book is inserted before trades at the same timestamp. New quotes cannot fill on the row that creates them. Paper quote arrival is no earlier than local poll receipt time plus simulated latency, so a decision made while processing a response cannot fill retroactively against trades already in that response. Offline CSV replay continues to use event time for decision arrival. This is an approximation: REST responses do not provide a synchronized book/trade stream, and the snapshots may miss intermediate book changes. Anchor overlap checks detect some window loss; they cannot prove feed completeness or exchange sequencing. The normalized CSV therefore represents a held-book model, not a reconstruction of the exchange order book.

A missing anchor, a polling gap beyond the freshness limit, an unalignable or late trade, or a transport failure cancels local orders. No catch-up fills are applied across the break. Recovery starts a new segment with a fresh book and anchor. Cash and inventory carry over and are marked at the recovered price. The dashboard shows the fault count, repeated-ID count, freshness age, and segment. Its freshness watchdog hides executable quotes while polling is stalled; ingestion also checks freshness before processing any trades. Network requests time out after five seconds and failed polls back off before retrying.

## Journal and CSV export

Each new run writes `paper.sqlite3` in its directory. SQLite batches retain:

- Original successful book/trade responses, and malformed response data when available.
- Normalized market rows, quote decisions, active/pending orders, fills, and resulting account balances.
- Feed status and faults, pause/resume actions, instrument, configuration, and execution settings.

A batch commits before its candidate account state is published. A storage error cancels local orders and permanently stops advancement for that run. Start a new run after correcting storage issues. Paper runs currently do not resume an account from a saved journal; the synthetic simulation's separate journal recovery remains supported.

Export committed market rows into independent CSV continuity segments:

```bash
python3 -m app.paper export data/paper-first/paper.sqlite3 data/paper-first-export
python3 -m app.backtest data/paper-first-export/market-0001.csv
```

The export directory must be new. `metadata.json` records capture settings and `manifest.json` lists segment row counts. A segment with only its bootstrap row is valid as a capture but cannot run through the backtester, which requires two rows. Do not concatenate segments across missing data. CSV exports contain market data, not account/control replay: each backtest starts a new account, and does not reproduce carried inventory or user pause/resume actions. The paper journal retains the actual paper decisions and balances. Backtest CLI settings must be supplied explicitly to match paper settings; in particular its default order size differs from the small BTC paper default.

Live monitoring retains 200 recent observations, decisions and fills in memory, while cumulative fill counts, fees and risk metrics span the run. The journal retains all committed rows. A run remains a paper-execution experiment; REST alignment and assumed queue/participation limit the meaning of its P&L. Tick/lot rules, exchange-specific priority, queue calibration, and WebSocket feed continuity remain future work.

## DNS and connectivity troubleshooting

Before starting a capture:

```bash
python3 -m app.network_check
```

The command creates no paper journal. It checks system DNS for the configured host, then validates public book and trade responses. It exits nonzero on failure and distinguishes DNS failures from API failures. To check an explicitly configured official regional endpoint, use the same `--base-url` option as the paper runner. Use the endpoint applicable to your deployment; changing regions is not an automatic retry policy.

`Temporary failure in name resolution` means the system resolver could not return an address; the HTTP request has not reached the exchange. The adapter reports this as `DNS lookup failed for <host>` and suspends paper execution. Compare resolution of the configured API hostname with an unrelated hostname, and inspect the local resolver:

```bash
getent ahosts openapi.okx.com
getent ahosts example.com
resolvectl status
```

On systems without `resolvectl`, inspect the operating system's network DNS settings. If unrelated hosts resolve but OKX does not, investigate DNS or network policy affecting that hostname with the network administrator. Application retries cannot repair an unavailable resolver. This application does not change system DNS settings, hard-code exchange IP addresses, or disable TLS verification.

Development checks found that both `www.okx.com` and `openapi.okx.com` failed DNS outside the sandbox while `example.com` resolved. The host reports upstream DNS `10.0.0.1`. Updating the default endpoint is correct according to OKX documentation, but it did not restore live connectivity in this environment.
