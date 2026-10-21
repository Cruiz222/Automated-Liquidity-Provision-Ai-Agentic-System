"""Read-only local operations summary from saved capture and workflow reports."""
import argparse
import json
from pathlib import Path
import shlex
import sqlite3
import time

from .experiments import DEFAULT_REGISTRY, read_runs


COLLECT_COMMAND = ['python3', '-m', 'app.paper', 'run', '--dns-mode', 'doh', '--duration-seconds', '1800']


def snapshot(data_dir='data', registry_path=DEFAULT_REGISTRY, active=None, clock=time.time):
    root = Path(data_dir).resolve()
    warnings, captures, workflows = [], [], []
    directories = []
    if root.exists():
        directories = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()),
                             key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = []
    for directory in directories:
        for name in ('capture-review.json', 'pipeline.json'):
            path = directory / name
            if path.is_file() and not path.is_symlink():
                candidates.append(path)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if len(candidates) > 100:
        warnings.append('Only the 100 most recently modified saved reports were inspected.')
    for path in candidates[:100]:
        try:
            if path.stat().st_size > 2_000_000:
                raise ValueError('report exceeds size limit')
            report = json.loads(path.read_text())
            common = {'report_path': str(path), 'saved_age_seconds': max(0, clock() - path.stat().st_mtime)}
            if path.name == 'capture-review.json':
                # Workflow review copies do not represent additional captures.
                if not (path.parent / 'paper.sqlite3').is_file():
                    continue
                if report['report_version'] != 1 or report['status'] not in ('more_data_needed', 'qualified_segments_available'):
                    raise ValueError('unsupported capture review')
                segments = report['segments']
                if not isinstance(segments, list) or not isinstance(report['qualified_segments'], list):
                    raise ValueError('invalid capture summary')
                captures.append(dict(common, journal=str((path.parent / 'paper.sqlite3').resolve()),
                    instrument=report['instrument'], status=report['status'],
                    qualified_segments=report['qualified_segments'], segment_count=len(segments),
                    failed_checks=sorted({check for segment in segments for check in segment['failed_checks']}),
                    simulated_fill_count=report['simulated_fill_count'], accounting_verified=report['accounting_verified']))
            else:
                if report['workflow_version'] != 1 or report['status'] not in ('running','completed','failed','interrupted','more_data_needed'):
                    raise ValueError('unsupported workflow state')
                workflows.append(dict(common, status=report['status'], phase=report['phase'],
                    error=report.get('error'), journal=report['journal'], experiment_id=report.get('experiment_id')))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            warnings.append(f'Saved report unavailable or malformed: {path}')
    try:
        runs = read_runs(registry_path, limit=20)
    except (OSError, sqlite3.Error, ValueError):
        runs = []
        warnings.append('Experiment registry unavailable.')
    latest_capture = captures[0] if captures else None
    latest_workflow = workflows[0] if workflows else None
    action = {'code': 'collect_data', 'label': 'Collect a longer independent market-data capture.',
              'command': shlex.join(COLLECT_COMMAND), 'artifact_path': None}
    active_summary = None
    if active:
        active_summary = {key: active.get(key) for key in ('mode', 'instrument', 'running', 'feed', 'storage_error')}
    if active and active.get('storage_error'):
        action = {'code': 'inspect_storage', 'label': 'Inspect the current journal error before starting another run.',
                  'command': None, 'artifact_path': None}
    elif active and active.get('feed') and active['feed']['status'] not in ('healthy', 'synchronizing', 'waiting'):
        action = {'code': 'check_feed', 'label': 'Check current public-feed connectivity.',
                  'command': 'python3 -m app.network_check --dns-mode doh', 'artifact_path': None}
    elif active and active.get('mode') == 'paper' and active.get('feed'):
        action = {'code': 'observe_capture', 'label': 'The public-feed session is open. Let the current capture finish before preparing its workflow.',
                  'command': None, 'artifact_path': None}
    elif latest_workflow and latest_workflow['status'] in ('failed', 'interrupted', 'running'):
        action = {'code': 'inspect_workflow',
                  'label': 'Inspect the saved workflow checkpoint; process liveness is unknown.' if latest_workflow['status'] == 'running' else 'Inspect the saved workflow failure or interruption.',
                  'command': None, 'artifact_path': latest_workflow['report_path']}
    elif latest_capture and latest_capture['qualified_segments']:
        if latest_workflow and latest_workflow['status'] == 'completed' and Path(latest_workflow['journal']).resolve() == Path(latest_capture['journal']).resolve():
            action['code'] = 'collect_independent_data'
            action['label'] = 'Collect a separate later capture before preparing validation or test studies.'
        else:
            action = {'code': 'review_capture_workflow', 'label': 'Run the offline workflow; its default data criteria will be checked again.',
                      'command': shlex.join(['python3', '-m', 'app.pipeline', latest_capture['journal']]),
                      'artifact_path': latest_capture['report_path']}
    return {'report_version': 1, 'data_dir': str(root), 'active': active_summary,
            'latest_capture': latest_capture, 'latest_workflow': latest_workflow,
            'recent_experiments': [{'id': r['id'], 'status': r['status'], 'created_at': r['created_at'],
                                    'instrument': r['manifest']['instrument']} for r in runs],
            'saved_capture_count': len(captures), 'saved_workflow_count': len(workflows),
            'next_action': action, 'warnings': warnings,
            'limitations': ['Saved reports are not revalidated here; run capture review or the workflow before using their data.',
                            'Saved ages are file modification ages, not live feed freshness.',
                            'A running checkpoint does not prove a worker is alive.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    args = parser.parse_args()
    try:
        print(json.dumps(snapshot(args.data_dir, args.registry), indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Operations summary failed: {error}\n')


if __name__ == '__main__':
    main()
