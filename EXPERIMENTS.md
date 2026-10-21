# Offline experiment registry

Run a frozen study through a local SQLite registry:

```bash
python3 -m app.experiments run data/study.lock.json
python3 -m app.experiments list
python3 -m app.experiments show RUN_ID
```

Replace `RUN_ID` with the full ID from the first command. The defaults are `data/experiments.sqlite3` for the registry and `data/experiments/<run-id>/` for artifacts. Each attempt records its frozen manifest, implementation hash, Python version, timestamps, terminal status, result path, result hash, and summary. Artifacts contain a frozen manifest copy and an evaluation report with experiment provenance. The runner is synchronous and local; it does not start a persistent job service, call a model, or contact an exchange.

## Reuse and repeats

The fingerprint covers the complete frozen manifest, hashes of the engine, importer, backtest, evaluation and experiment-runner source files, and Python version. Input datasets and frozen metadata are verified before checking for reuse. If a matching completed study exists and its report artifact still matches its saved hash, the command returns that run with `reused: true` without recalculating evaluation.

Use `--repeat` for a separate attempt with a new ID:

```bash
python3 -m app.experiments run data/study.lock.json --repeat
```

Changed dataset bytes fail validation; they cannot silently reuse a result. Changed implementation files or interpreter versions create a different fingerprint. Changed report artifacts are rejected rather than trusted or overwritten. An explicit repeat creates new artifacts without modifying the original result.

Files are written exclusively in new per-run directories, flushed and synced before completion is recorded. SQLite stores run transitions transactionally. A partial or failed artifact is never reused as a completed report. This is not one atomic transaction across the filesystem and database: a crash may leave partial files or a run marked running. Missing or changed completed artifacts also require inspection or an explicit repeat. Hashes detect ordinary consistency changes and are not signatures or a complete environment lock.

## Failure and interruption records

An identical fingerprint can have at most one `running` record, enforced by a unique SQLite index. Computation failures and invalid frozen inputs are recorded as failed attempts when the registry can write them. Ctrl+C during a registered execution records an interruption. A hard-killed process may leave `running` behind; after confirming that worker has stopped:

```bash
python3 -m app.experiments mark-interrupted RUN_ID --reason "Worker stopped before completion"
```

This updates metadata only. It does not stop a process or cancel execution. Completed, failed and interrupted records cannot be rewritten through that command. There is no automatic restart or automatic stale-worker detection. Errors opening the registry or checking an existing cached artifact can occur before a new attempt is created.

## Dashboard

The existing simulation and paper dashboards now include an offline experiment list. It refreshes every five seconds and shows run status, instrument, start time and an Inspect button. Details show provenance, failure information, the report path and scenario/role summaries. These routes are read-only:

- `GET /api/experiments` lists up to 50 recent attempts.
- `GET /api/experiments/<run-id>` reads one attempt.

An absent registry returns an empty list without creating a database. Database errors report unavailable status. Text from experiment metadata is inserted as text rather than HTML. Browsing cannot launch or modify experiments. The dashboard remains local to `127.0.0.1` and has no multi-user access control.

Every command accepts `--registry`; the run command also accepts `--output-dir`. Point the synthetic server or paper runner at another registry with its own `--registry` option. Timestamps are stored in UTC; the dashboard displays them in the browser's local time.

This registry tracks evidence; it does not qualify data, choose a profitable scenario, or authorize live trading. Continue using capture review, frozen role separation, accounting audits and the bounded research proposal workflow.
