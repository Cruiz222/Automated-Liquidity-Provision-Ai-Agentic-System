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

Trades are replayed chronologically against the most recent captured book with a timestamp no later than the trade. The newest accepted trade timestamp is the execution watermark: snapshots ahead of it remain buffered rather than advancing the paper clock. A later poll can therefore supply trades older than its newest book without causing a false gap. Before the segment's bootstrap snapshot, delayed trades are counted as warm-up skips and cannot fill. The book is held constant between captured snapshots. A fresh book is inserted before trades at the same timestamp. New quotes cannot fill on the row that creates them. Paper quote arrival is no earlier than local poll receipt time plus simulated latency, so a decision made while processing a response cannot fill retroactively against trades already in that response. Offline CSV replay continues to use event time for decision arrival. This is an approximation: REST responses do not provide a synchronized book/trade stream, and the snapshots may miss intermediate book changes. Anchor overlap checks detect some window loss; they cannot prove feed completeness or exchange sequencing. The normalized CSV therefore represents a held-book model, not a reconstruction of the exchange order book.

A missing anchor, a polling gap beyond the freshness limit, an unalignable or late trade, or a transport failure cancels local orders. No catch-up fills are applied across the break. Recovery starts a new segment with a fresh book and anchor. Cash and inventory carry over and are marked at the recovered price. The dashboard shows the fault count, repeated-ID count, freshness age, segment, buffered-book count, and alignment lag. Alignment lag is the latest captured book timestamp minus the last processed execution event timestamp, clamped to zero; it is distinct from transport freshness. When trades pause, the execution price and account mark remain at the last processed event while newer books wait in the buffer. Its freshness watchdog hides executable quotes while polling is stalled; ingestion also checks freshness before processing any trades. Network requests time out after five seconds and failed polls back off before retrying.

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

Live monitoring retains 200 recent observations, decisions and fills in memory, while cumulative fill counts, fees and risk metrics span the run. The journal retains all committed rows and raw responses, including books that have not yet crossed the trade watermark. CSV exports include only processed events; they do not flush unaligned books at shutdown. The buffer retains at most 512 snapshots, keeping an alignment predecessor and a recent freshness window. Missing earlier coverage still triggers resynchronization. A run remains a paper-execution experiment; REST alignment and assumed queue/participation limit the meaning of its P&L. Tick/lot rules, exchange-specific priority, queue calibration, and WebSocket feed continuity remain future work.

## No-sudo DNS-over-HTTPS mode

When the local DNS resolver refuses OKX lookups, enable the application-only resolver:

```bash
python3 -m app.network_check --dns-mode doh
python3 -m app.paper run --dns-mode doh --polls 30
```

This requires no sudo, installations, or changes to Wi-Fi, system DNS, `/etc/hosts`, or other processes. System DNS remains the default. The selected `dns_mode` is saved in run metadata.

In this mode only the configured OKX hostname is resolved using [Cloudflare's DNS-over-HTTPS JSON interface](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/) at `https://1.1.1.1/dns-query`. Both the resolver and OKX connections use normal TLS verification. OKX's original hostname remains the HTTP Host header, TLS SNI, and certificate verification target. Exchange addresses are discovered dynamically and cached for at most their DNS TTL (including CNAME TTLs), capped at five minutes. The resolver accepts public IPv4 addresses for the queried name or its CNAME chain and tries the returned addresses on connection failure.

The opt-in transport uses direct HTTPS connections, ignores environment proxy settings, restricts requests to the configured OKX hostname on port 443, and rejects redirects. It has no automatic fallback to insecure TLS, fixed exchange addresses, or another regional API. Outbound HTTPS to Cloudflare and OKX must be available. No global socket functions are patched.

Development verification: DNS-over-HTTPS resolved OKX and retrieved valid book and trade responses without sudo in the environment where router DNS returned REFUSED. Standard system DNS remains affected until the host network is repaired.

## DNS and connectivity troubleshooting

Before starting a capture:

```bash
python3 -m app.network_check
```

The command creates no paper journal. Add `--dns-mode doh` to test the no-sudo alternative described above. It checks system DNS for the configured host, then validates public book and trade responses. It exits nonzero on failure and distinguishes DNS failures from API failures. To check an explicitly configured official regional endpoint, use the same `--base-url` option as the paper runner. Use the endpoint applicable to your deployment; changing regions is not an automatic retry policy.

`Temporary failure in name resolution` means the system resolver could not return an address; the HTTP request has not reached the exchange. The adapter reports this as `DNS lookup failed for <host>` and suspends paper execution. Compare resolution of the configured API hostname with an unrelated hostname, and inspect the local resolver:

```bash
getent ahosts openapi.okx.com
getent ahosts example.com
resolvectl status
```

On systems without `resolvectl`, inspect the operating system's network DNS settings. If unrelated hosts resolve but OKX does not, investigate DNS or network policy affecting that hostname with the network administrator. Application retries cannot repair an unavailable resolver. This application does not change system DNS settings, hard-code exchange IP addresses, or disable TLS verification.

Development checks found that both `www.okx.com` and `openapi.okx.com` failed DNS outside the sandbox while `example.com` resolved. The host reports upstream DNS `10.0.0.1`. Updating the default endpoint alone did not restore live connectivity; the opt-in application-only DoH transport did.

## Alignment regression validation

The `trade-watermark-v1` alignment model was replayed against two completed 30-poll captures using their original receipt timestamps and configuration. Their original gap counts were 19 and 18; both became zero and ended healthy with watermark buffering. This validates the handling of those recorded timing patterns, not feed completeness or strategy profitability. Tests also cover delayed warm-up trades, monotonic execution timestamps, buffered snapshots, durable batch failure, genuine missing-anchor gaps, and stale polling.

A fresh 30-poll DoH check accepted all 30 polls, ended healthy, and exported 1,154 processed rows in two replay-validated segments. It recorded one missing-anchor reset and no book/trade alignment resets. There were no fills, so this check validates capture and alignment rather than strategy performance. Local detailed evidence is saved in `data/alignment-validation.json` and the raw run journal in `data/paper-6d3d79ee38/paper.sqlite3`.

## Execution diagnostics and account audit

New paper batches record one execution outcome for each processed trade. The dashboard shows cumulative counts. Outcomes distinguish filled orders, prices that never reached the limit, quotes still awaiting latency, post-only rejections, no active quote on the relevant side, queue-ahead consumption, exhausted orders, account limits, negligible volume, and user pause. Book-only rows do not count as trade outcomes. These describe the local execution model, not exchange-verified fills.

Audit a completed run without network access or changes to the journal:

```bash
python3 -m app.paper_report data/paper-6d3d79ee38/paper.sqlite3
python3 -m app.paper_report data/paper-6d3d79ee38/paper.sqlite3 --output data/paper-audit-new.json
```

The report includes outcome counts, classification provenance, per-segment activity, processed-row spread statistics, distance of missed trades from active limits, final account state, P&L, drawdown, and feed fault occurrences. It records a digest of committed batch sequence numbers and payloads. Every processed account balance is reconciled against recorded fills, configured fees, and the row's midpoint; impossible balances, incorrect fees, or batch sequence gaps fail the audit. This is accounting consistency checking, not cryptographic authentication or a full execution replay. Output paths must be new.

Older runs have no explicit outcome field. Their classifications are labelled `inferred_from_saved_state`; reachable but unfilled historical quotes remain `unfilled_reachable_quote` rather than assigning an unobserved queue or latency cause. Rows with no active quote cannot establish why it was absent. Gap-skipped and warm-up trades are outside the processed trade totals.

The 30-poll alignment-validation run had 1,126 processed trades: 346 did not reach a saved active limit, and 780 had no active quote on the relevant side. It had no fills and its account reconciled. The configured half spread was 12 bps, while the processed-row mean full market spread was about 0.01188 bps; these figures describe different widths and do not establish profitable quoting settings. Execution settings remain unchanged pending longer data collection and calibration to observed fills.

## Timed collection

Use `--duration-seconds` to bound longer runs; a normal stop writes `capture-review.json` automatically. See [timed collection and capture review](COLLECTION.md) for limits, per-segment research criteria, configurable thresholds, and automation exit codes.

## Optional streaming transport

Use `--transport websocket` with the project-local pinned dependency for bounded event-time reordering instead of REST trade-watermark waiting. See [WebSocket setup and continuity limits](WEBSOCKET_FEED.md). REST remains available for comparison, and neither path guarantees complete exchange sequencing.
