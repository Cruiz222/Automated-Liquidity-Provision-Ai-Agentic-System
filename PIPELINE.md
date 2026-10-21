# Completed-capture research workflow

The offline pipeline connects the existing modules into one command:

```bash
python3 -m app.pipeline data/paper-first/paper.sqlite3
```

Run it after collection has stopped. It creates a new unique `data/workflow-...` directory by default. Use `--output-dir` for an explicit new workspace and `--registry` for an alternate experiment registry. Existing workspace directories are refused without overwriting their contents. The command makes no network requests, model calls, exchange calls, or live-agent changes.

## Phases

1. Audit the completed capture and review its segments against the declared data criteria.
2. Stop with `more_data_needed` if no segment qualifies, retaining the review and workflow state.
3. Export the capture into continuity CSVs and verify that the journal did not change during export.
4. Select only qualified segments for a reference development study, using the capture's configuration and execution assumptions.
5. Freeze the reference manifest and generate the deterministic offline research proposal.
6. Revalidate proposal evidence and materialize the reference plus fixed execution-stress scenarios.
7. Run the study through the experiment registry and record its run ID and report path.

All generated study datasets have the `development` role. The export preserves separate files for every capture segment, but only qualifying ones enter the study. Validation and test studies remain separate explicit research work. No model or metric selects a winning configuration. The offline proposer is used deliberately; this command does not enable the optional API provider.

## Outputs and failure handling

`pipeline.json` records effective criteria, source journal digest, workflow source hash, current status/phase, timestamped phase history, artifact paths and the registered experiment ID. It is atomically replaced at phase checkpoints. Other generated artifacts include:

- `capture-review.json`.
- Segmented CSVs, source metadata and export manifest under `market/` when data qualifies.
- `reference-plan.json` and `reference.lock.json`.
- `proposal.json` and `study.lock.json`.
- Registered study results under `experiments/<run-id>/`.

A short capture produces only its review and state; no experiment is registered. Ordinary failures record the failed phase and error, preserving earlier artifacts. Ctrl+C records an interrupted checkpoint when storage remains available. A hard kill or storage failure can leave the last checkpoint marked running or incomplete. There is no automatic workspace resume or background job service, and the workflow is not one transaction across all files and the registry.

Inspect partial workspaces and the registry before retrying. To reevaluate an already materialized study, use the existing frozen manifest with `app.experiments run`; its verified reuse rules apply. A new pipeline workspace creates new frozen paths and should be treated as a new preparation attempt, not an idempotent rerun of the earlier workflow. Source journals are never modified.

Exit codes are 0 for a completed registered evaluation, 2 for `more_data_needed`, 1 for ordinary errors, and 130 for Ctrl+C. Each completed study remains visible through the existing read-only dashboard experiment list.

## Criteria and interpretation

Defaults match capture review: at least five minutes and 1,000 processed trades per segment, at least two rows starting with a book snapshot, at most 5,000 ms event spacing and alignment lag, and at least 90% healthy polls. Use the same explicit review options to choose other research policy:

- `--min-duration-seconds`
- `--min-trades`
- `--max-event-gap-ms`
- `--max-alignment-lag-ms`
- `--min-healthy-fraction`

Effective values remain in all review evidence and checkpoints. The pipeline does not lower thresholds automatically or combine separated segments to meet them. Passing these data criteria is not proof of complete market data, calibrated execution, independent holdout data, or profitable trading. Do not lower thresholds merely to present a short sample as sufficient evidence.

The saved short live capture at `data/paper-6d3d79ee38/paper.sqlite3` stops at `more_data_needed` under the defaults. Qualifying test fixtures cover the entire workflow through four registered scenario evaluations. For actual longer data collection:

```bash
python3 -m app.paper run --dns-mode doh --duration-seconds 1800
```

Then pass that completed run's printed journal path to the pipeline. Longer collection can still produce only short segments if feed breaks frequently; the review makes that visible.
