# WebSocket capture and timing diagnosis

The public WebSocket path uses OKX `books5` snapshots and aggressor-side `trades` messages on `wss://ws.okx.com/ws/v5/public` over port 443. It uses no credentials and shares the existing local paper execution, journal, CSV export, account audit, quality review and offline workflow.

## Run without sudo

The optional transport requires Python 3.11+ and the pinned `websockets` dependency. Core REST and offline tools still require no third-party packages. Set up a project-local environment if it does not exist:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-websocket.txt
```

Then collect a longer streaming run:

```bash
.venv/bin/python -m app.paper run --transport websocket --dns-mode doh --duration-seconds 1800
```

The dashboard remains http://127.0.0.1:8000. A fresh run directory is generated automatically. Use `--no-dashboard` for terminal-only capture. Normal shutdown saves the capture review and prints an offline-review command with the actual journal path, ready to copy. No placeholder replacement is needed.

The optional DoH mode resolves only `ws.okx.com` through the existing process-local resolver. The WebSocket library wraps the resulting raw socket with a default verified SSL context using `ws.okx.com` for hostname verification and SNI. No sudo, system DNS modification, fixed exchange IP or disabled TLS verification is used. The public WebSocket transport makes direct connections and disables automatic proxy selection. REST remains available as the default `--transport rest` path for comparison.

The [OKX API documentation](https://www.okx.com/docs-v5/en/) describes the public book/trade subscriptions and application ping/pong. The [WebSocket client reference](https://websockets.readthedocs.io/en/latest/reference/sync/client.html) documents the supplied socket and TLS hostname options used here.

## Bounded event-time ordering

WebSocket source timestamps can arrive out of order across channels. Events are held for `--reorder-ms` (default 500), sorted by source timestamp, and processed when local time passes their timestamp plus that interval. Books precede trades at the same timestamp, matching the normalized CSV convention. The paper clock no longer waits for the next trade before processing an eligible book. Quotes become available no earlier than batch receipt plus configured execution latency, so buffered historical trades cannot fill a decision retroactively.

This delay is an explicit ordering assumption, not a guarantee about exchange ordering. A trade that arrives behind the already-processed clock fails closed: local quotes are cancelled and the connection is restarted. The same applies to stale or future events, book-sequence regression, malformed messages, a full reorder buffer, missing initial book, and transport or heartbeat failure. The book freshness limit remains unchanged. An idle but connected book can still exceed that conservative source-freshness policy.

Book snapshots do not provide a complete incremental order-book sequence chain. Trade IDs are deduplicated within a bounded window without assuming numeric adjacency; an unseen regressing ID requires resynchronization. This is not a reconstruction of every exchange update. Raw received messages, connection epoch, normalized events, quote decisions, fills and account results are journaled transactionally. No candidate fills or buffer state become visible before the batch commits.

## Heartbeats, reconnects and bounds

The client subscribes to both channels, checks subscription acknowledgements, uses protocol-level heartbeat support, and sends OKX application `ping` after inactivity. Missing application activity eventually closes the connection. Transport faults are logged, quotes are cancelled, and subsequent attempts reconnect after bounded failure backoff. A new connection epoch starts from a fresh snapshot and continuity segment; inventory and cash carry over. Resume after a user pause also waits for a fresh processed snapshot.

Receive batches are bounded at 2,000 messages, the reorder buffer at 5,000 events, and the recent trade-ID cache at 4,096 IDs. Background receive queues apply backpressure. Capacity exhaustion produces a fault instead of silently dropping data. Stream polling already waits for data, so it does not add the REST poll-interval sleep on healthy iterations. Duration limits still allow an in-flight connection/request to finish. Trailing buffered events are retained in raw batches but are not forced through execution at shutdown.

## Timing evidence

Analyze a completed journal without changing it:

```bash
python3 -m app.feed_timing data/paper-0add54aa17/paper.sqlite3
```

The timing report covers processed event-spacing violations, alignment-lag violations including batches with no processed rows, book age, receipt gaps and REST watermark waiting. It cannot distinguish a genuinely quiet trade stream from cached or delayed REST trade responses.

The inspected REST run had 440 polls, a 5,525 ms maximum processed event gap and 11,925 ms maximum book/execution alignment lag. During high-lag batches, books were fresh but the trade anchor was unchanged and no rows were processed. This identifies waiting in the old trade-watermark model, not proof of missing exchange trades or a network outage. Detailed local evidence is `data/paper-0add54aa17/timing-analysis.json`.

A fresh 20-second WebSocket smoke check accepted 36 batches and ended healthy with zero continuity faults. It exported and replay-validated 149 processed rows, including 43 trades; accounting reconciled. Maximum event spacing was 600 ms and maximum alignment lag 800 ms. The saved report still failed duration and trade-count criteria, as expected for a short check. This is not a matched-market long-run comparison or proof that a 30-minute stream will qualify. Local evidence is `data/websocket-validation.json`.

No quality thresholds, fee assumptions or quoting parameters were weakened. Continue with a longer independent stream capture and inspect its own saved quality review before evaluation.
