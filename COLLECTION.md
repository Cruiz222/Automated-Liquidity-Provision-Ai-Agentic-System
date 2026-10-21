# Timed collection and capture review

Collect public data with a duration limit, retaining the existing read-only exchange adapter, simulated execution and atomic journal:

```bash
python3 -m app.paper run --dns-mode doh --duration-seconds 1800
```

This runs for approximately 30 minutes with the local dashboard at http://127.0.0.1:8000. A unique run directory is generated automatically. Add `--no-dashboard` for terminal-only capture. The process stays attached to the terminal; it is not a background service and does not survive logout or reboot.

If both `--polls` and `--duration-seconds` are provided, the first limit reached stops collection. The duration uses a monotonic clock. Sleeps and failure backoff are clipped to the remaining time. A request already in progress is allowed to finish after the deadline. Ctrl+C closes the journal cleanly. A forced kill leaves previously committed SQLite batches available, but may skip the final report.

After a normal bounded stop or Ctrl+C, the runner writes `capture-review.json` in its new run directory. A bounded run still exits nonzero if no fresh poll was accepted. A short capture can complete successfully while its report says `more_data_needed`; these are separate outcomes.

## Review a completed journal

```bash
python3 -m app.capture_review data/paper-first/paper.sqlite3
python3 -m app.capture_review data/paper-first/paper.sqlite3 --output data/capture-review-new.json
```

Run this after stopping collection. Review checks accounting using the existing paper audit, validates normalized market rows, and checks that the journal stayed unchanged while both checks ran. It reads the journal without modifying it. Existing output paths are never overwritten.

Default research-data criteria apply separately to each continuity segment:

| Criterion | Default |
| --- | --- |
| Processed duration | At least 300 seconds |
| Processed trades | At least 1,000 |
| Rows | At least two, starting with a book-only snapshot |
| Maximum gap between processed event timestamps | At most 5,000 ms |
| Healthy poll fraction | At least 90% |
| Maximum observed alignment lag | At most 5,000 ms |

Alignment lag must be observable from saved successful poll metadata. Poll fractions include feed-fault attempts; initial synchronization polls are not healthy polls. Faults before the first segment count toward run totals, while later faults count against the most recent segment. Pausing simulated orders does not stop capture or make a healthy public feed unhealthy.

These thresholds are explicit, configurable research policy. They are not statistically calibrated guarantees. Event spacing may reflect an idle market and does not prove the presence or absence of missing trades. A segment is `qualified` only when every check passes; the report lists failed checks otherwise. The run status is `qualified_segments_available` if at least one segment passes, or `more_data_needed` if none do. It does not merge gaps, certify all segments, or assess profitability.

For different criteria, use `--min-duration-seconds`, `--min-trades`, `--max-event-gap-ms`, `--max-alignment-lag-ms`, and `--min-healthy-fraction`. Their effective values are recorded in the report. Add `--require-qualified` for an automation-friendly exit status: 0 if a segment qualifies, 2 if more data is needed, and 1 on a review error. Without that option, a valid review exits successfully even when no segment qualifies.

After collecting sufficient data, export separate continuity CSVs and use qualifying segments in a frozen evaluation plan. Keep development, validation and test periods separate. Simulated fill count is reported independently: passing data criteria does not provide real exchange fill observations or calibrate queue position. Captures already inspected during development are not untouched holdout data.
