"""Offline completed-capture to registered development-study workflow."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .capture_review import Criteria, review
from .evaluate import freeze, write_new
from .experiments import DEFAULT_REGISTRY, run_study
from .paper_store import export_csv
from .research_agent import materialize, propose


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def run_pipeline(journal, output_dir, registry_path=DEFAULT_REGISTRY, criteria=None):
    criteria = criteria or Criteria()
    source = Path(journal).resolve()
    if not source.is_file():
        raise ValueError(f'Capture journal not found: {journal}. Use the printed paper.sqlite3 path from a completed capture; ACTUAL_JOURNAL_PATH is a placeholder. Find saved captures with python3 -m app.operations.')
    workspace = Path(output_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    state = {'workflow_version': 1, 'status': 'running', 'phase': 'initializing',
             'created_at': timestamp(), 'journal': str(source), 'workspace': str(workspace),
             'registry': str(Path(registry_path).resolve()), 'criteria': asdict(criteria),
             'pipeline_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'artifacts': {}, 'experiment_id': None, 'error': None, 'history': []}

    def checkpoint(phase, status='running', error=None):
        state.update(phase=phase, status=status, updated_at=timestamp(), error=error)
        state['history'].append({'phase': phase, 'status': status, 'at': state['updated_at']})
        temporary = workspace / ('.state-' + uuid.uuid4().hex)
        with temporary.open('x') as target:
            json.dump(state, target, indent=2, allow_nan=False)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, workspace / 'pipeline.json')

    def save(name, value):
        path = workspace / name
        write_new(path, value)
        state['artifacts'][name] = str(path)
        return path

    try:
        checkpoint('reviewing_capture')
        capture = review(source, criteria)
        review_path = save('capture-review.json', capture)
        state['source_batches_sha256'] = capture['source']['committed_batches_sha256']
        if not capture['qualified_segments']:
            checkpoint('reviewing_capture', 'more_data_needed')
            return state
        checkpoint('exporting_segments')
        export_dir = workspace / 'market'
        export_csv(source, export_dir)
        state['artifacts']['market'] = str(export_dir)
        # Confirm source consistency after the separate snapshot used by export.
        current = review(source, criteria)
        if current['source']['committed_batches_sha256'] != state['source_batches_sha256']:
            raise ValueError('Capture changed during export; stop collection before running the pipeline')
        metadata = json.loads((export_dir / 'metadata.json').read_text())
        plan = {'version': 1, 'instrument': capture['instrument'], 'config': metadata['config'],
                'datasets': [{'name': 'segment-' + segment, 'path': str(export_dir / f'market-{int(segment):04d}.csv'),
                              'role': 'development'} for segment in capture['qualified_segments']],
                'scenarios': [{'name': 'reference', 'execution': metadata['execution']}]}
        checkpoint('freezing_reference')
        plan_path = save('reference-plan.json', plan)
        manifest = freeze(plan_path)
        manifest_path = save('reference.lock.json', manifest)
        checkpoint('generating_offline_proposal')
        proposal = propose(review_path, manifest_path, provider='offline')
        save('proposal.json', proposal)
        checkpoint('materializing_study')
        study = materialize(proposal, manifest)
        study_path = save('study.lock.json', study)
        state['study_manifest_sha256'] = study['manifest_sha256']
        checkpoint('evaluating_study')
        experiment = run_study(study_path, registry_path, workspace / 'experiments')
        state['experiment_id'] = experiment['id']
        state['artifacts']['experiment_report'] = experiment['result_path']
        state['experiment_reused'] = experiment['reused']
        checkpoint('finished', 'completed')
        return state
    except BaseException as error:
        try:
            checkpoint(state['phase'], 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                       str(error) or type(error).__name__)
        except OSError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('--output-dir', type=Path, default=Path('data') / ('workflow-' + uuid.uuid4().hex[:10]))
    parser.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument('--min-duration-seconds', type=int, default=300)
    parser.add_argument('--min-trades', type=int, default=1000)
    parser.add_argument('--max-event-gap-ms', type=int, default=5000)
    parser.add_argument('--max-alignment-lag-ms', type=int, default=5000)
    parser.add_argument('--min-healthy-fraction', type=float, default=.9)
    args = parser.parse_args()
    try:
        criteria = Criteria(args.min_duration_seconds * 1000, args.min_trades, args.max_event_gap_ms,
                            args.max_alignment_lag_ms, args.min_healthy_fraction)
        print(f'Offline workflow workspace: {args.output_dir}', flush=True)
        result = run_pipeline(args.journal, args.output_dir, args.registry, criteria)
        print(json.dumps({'status': result['status'], 'phase': result['phase'],
                          'workspace': result['workspace'], 'experiment_id': result['experiment_id']}, indent=2))
    except KeyboardInterrupt:
        parser.exit(130, 'Offline workflow interrupted; inspect pipeline.json in the workspace.\n')
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.exit(1, f'Offline workflow failed: {error}\n')
    if result['status'] == 'more_data_needed':
        parser.exit(2)


if __name__ == '__main__':
    main()
