# Bounded research proposer

The research module reads a completed capture review and a frozen study manifest, optionally with its evaluation report. It produces a reviewable proposal. It has two modes: a deterministic offline template and an optional OpenAI structured-output adapter. The quoting strategy remains rule-based. Neither mode can place orders, change the live agent, run shell commands, select files, deploy settings, or execute its own proposal.

## Generate a proposal offline

```bash
python3 -m app.research_agent propose --capture-review data/paper-6d3d79ee38/capture-quality.json --manifest data/captured-study.lock.json --evaluation data/captured-study-report.json --output data/research-proposal-new.json
```

Use your own completed capture review and matching frozen manifest. All output paths must be new. The command rechecks the journal and capture criteria, verifies frozen input hashes, and records evidence hashes, the base manifest digest, catalog digest, generator identity and proposal digest. It resolves evidence file paths for later checks. Evidence must remain available and unchanged.

If no segment qualifies under the review's declared criteria, the only accepted action is `collect_more_data`, with no scenarios. A qualifying capture does not establish profitability or actual exchange execution calibration. Criteria are research policy, not model-certified readiness.

When qualified segments exist, each development CSV in the base manifest must match the normalized market rows of a qualifying journal segment. This prevents borrowing unrelated qualifying evidence. A proposal may choose only from this fixed catalog:

| ID | Offline stress |
| --- | --- |
| `latency-stress` | Latency at least 300 ms |
| `queue-stress` | Participation at most 5%, queue ahead at least 0.001 base units |
| `fee-stress` | Fee at least 3 bps |

These are illustrative stresses, not recommended exchange estimates. Materialization retains a reference scenario and preserves any base assumption already more restrictive than a stress. It cannot change instrument, cash, inventory limits, order size, quote spread or inventory skew. Numeric configuration never comes from model output.

## Optional model generation

The adapter uses the [OpenAI Responses API with Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses). Configure `OPENAI_API_KEY` in the process environment and choose a model available to your account that supports structured outputs:

```bash
python3 -m app.research_agent propose --capture-review CAPTURE_REVIEW.json --manifest BASE.lock.json --provider openai --model YOUR_MODEL_ID --output data/model-proposal-new.json
```

The uppercase paths and model ID are placeholders. The request sends only a curated context: instrument, capture criteria, qualifying-segment count, development dataset count, the fixed catalog, and development metrics if an evaluation report is supplied. It excludes validation/test result rows, dataset paths, raw journal responses and credentials from the model input. Evaluation evidence must reference the same frozen manifest. It cannot establish that the human has never inspected a holdout.

One explicit request is made, without tools or automatic retries, with a 1,200-output-token cap, 50,000-character input limit, `store: false`, normal TLS verification, and disabled redirects. Requests use system networking; the OKX-only DoH transport is not reused for API credentials. API usage may be billed to the configured account. Keys are read from the environment and not stored in proposal files. HTTP errors are reported without response bodies or credential values.

Refused, incomplete, malformed, extra-field or out-of-catalog responses fail before a proposal is accepted. Structured output and local validation constrain actions and data shape; they do not guarantee the factual correctness of free-text rationale. Live model generation has not been verified in this environment because no API key is configured. Tests use mocked responses, including failures.

## Materialize and evaluate separately

After inspecting a `propose_study` proposal:

```bash
python3 -m app.research_agent materialize data/proposal.json --manifest data/base.lock.json --output data/proposed-study.lock.json
python3 -m app.evaluate run data/proposed-study.lock.json --output data/proposed-study-report.json
```

Materialization rechecks proposal, catalog, base manifest, evidence files and journal. It emits a new frozen manifest with only the original development datasets, the reference scenario, the selected stresses, and proposal provenance. Validation and test datasets stay out of the generated study. It does not run evaluation or modify the base manifest. The separate evaluate command remains the execution step.

A `collect_more_data` proposal cannot materialize a study. The current short-capture demonstration returns that action; it is not bypassed to demonstrate a model experiment. Qualifying fixtures exercise the complete proposal-to-evaluation path in tests.

Hashes provide consistency and provenance, not signatures. Changing criteria, manually regenerating hashes, or reusing inspected data remains a human research decision and must not be represented as an untouched holdout result.
