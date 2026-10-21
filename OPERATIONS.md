# Local operations view

The existing dashboard now includes an Operations panel showing a next action, the latest saved capture review, latest workflow checkpoint, and recent experiment attempts. Start the synthetic dashboard with `python3 -m app.server`, or use the existing paper runner. Restart an already-running server to load the new endpoint.

The panel is read-only. It cannot execute its suggested command, launch a collector, update a workflow, call a model, or change live settings. Commands are displayed as text and use discovered journal paths, with shell quoting for paths containing spaces. Metadata and errors are inserted as text rather than HTML.

## Command-line summary

```bash
python3 -m app.operations
python3 -m app.operations --data-dir data --registry data/experiments.sqlite3
```

The CLI reads saved artifacts only. The dashboard additionally supplies current in-process feed and storage health through `GET /api/operations`. An absent data directory or registry yields an empty summary and does not create files or databases. A corrupt saved report is skipped with an explicit warning; registry failure is reported separately so available capture information remains visible.

Both server modes accept `--data-dir` for the saved-report root and `--registry` for experiment metadata. These options configure the panel; `--data-dir` does not change the paper runner's capture output directory. Put run/workflow directories directly beneath the configured root for discovery.

## Decision order

1. Current storage errors request inspection of the journal error.
2. A currently unhealthy public feed requests a read-only connectivity check.
3. An open paper session recommends waiting for that capture to finish, avoiding a duplicate collector.
4. A saved failed, interrupted or running workflow points to its checkpoint for inspection. A saved running state does not establish that a process is alive.
5. A saved qualifying capture suggests its concrete offline-workflow command, which rechecks default data criteria.
6. A completed workflow for that qualifying capture suggests collecting separate later data. Other cases suggest longer collection.

This is workflow guidance, not trading advice or a deployment authorization. When saved criteria differ from workflow defaults, the suggested workflow may still stop with `more_data_needed`; it will not silently weaken thresholds or inherit an unchecked policy.

## Discovery and freshness

The view inspects immediate, nonsymlink subdirectories for `capture-review.json` beside `paper.sqlite3`, and for `pipeline.json`. Review copies inside workflow directories do not count as new captures. It reads at most the 100 most recently modified candidate reports, with a two-megabyte file-size limit, and up to 20 recent registered experiment attempts. Counts describe the inspected saved reports, not all historical activity or active processes.

File modification order selects the latest discovered reports. Saved ages describe file modification times, not source-market age, current feed health, proof of journal validity, or worker liveness. The operations view does not rerun accounting checks or verify that a journal is unchanged. Use capture review or the offline workflow to revalidate evidence before evaluation. The live feed panel remains the source for current connection freshness.

Malformed, absent or stale saved artifacts do not justify assuming a successful experiment. The operations endpoint returns a partial summary with warnings where possible, or HTTP 503 if discovery itself fails. The frontend retries every five seconds. All data stays local to the existing localhost server; no multi-user authentication or remote deployment is added by this feature.

When the pinned WebSocket package is present in this project’s `.venv`, suggested collection commands use that interpreter and `--transport websocket`. Otherwise they retain the dependency-free REST command. This only changes the displayed suggestion; the operations endpoint never launches it.
