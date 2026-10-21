"""Freeze and run offline execution-sensitivity studies across independent captures."""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import platform

from .backtest import Execution, compare
from .engine import Config
from .market_data import parse_csv


ROLES = ('development', 'validation', 'test')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def check_datasets(datasets):
    names, hashes = set(), set()
    for dataset in datasets:
        if not dataset['name'] or dataset['name'] in names:
            raise ValueError('Dataset names must be nonempty and unique')
        if dataset['sha256'] in hashes:
            raise ValueError('Duplicate dataset content cannot be counted twice')
        if dataset['role'] not in ROLES:
            raise ValueError('Dataset role must be development, validation, or test')
        names.add(dataset['name'])
        hashes.add(dataset['sha256'])
    for index, left in enumerate(datasets):
        for right in datasets[index + 1:]:
            if max(left['start_ms'], right['start_ms']) <= min(left['end_ms'], right['end_ms']):
                raise ValueError('Dataset time ranges overlap across roles or captures; use one consistent epoch time base')


def fields(item, allowed):
    if not isinstance(item, dict) or set(item) - set(allowed):
        raise ValueError('Unknown fields or invalid object in evaluation plan')


def freeze(path):
    path = Path(path)
    plan = json.loads(path.read_text())
    fields(plan, ('version', 'instrument', 'config', 'datasets', 'scenarios'))
    if plan['version'] != 1:
        raise ValueError('Unsupported study version')
    if not isinstance(plan['instrument'], str) or not plan['instrument'].strip():
        raise ValueError('An instrument identifier is required')
    config = Config(**plan.get('config', {}))
    if not plan['datasets'] or not plan['scenarios']:
        raise ValueError('At least one dataset and scenario are required')
    datasets = []
    for item in plan['datasets']:
        fields(item, ('name', 'path', 'role'))
        source = (path.parent / item['path']).resolve()
        raw = source.read_bytes()
        events = parse_csv(raw.decode('utf-8-sig'))
        datasets.append({'name': item['name'], 'path': str(source), 'role': item['role'],
                         'sha256': hashlib.sha256(raw).hexdigest(), 'event_count': len(events),
                         'start_ms': events[0].timestamp_ms, 'end_ms': events[-1].timestamp_ms})
    check_datasets(datasets)
    scenarios, names = [], set()
    for item in plan['scenarios']:
        fields(item, ('name', 'execution', 'fee_bps'))
        if not item['name'] or item['name'] in names:
            raise ValueError('Scenario names must be nonempty and unique')
        names.add(item['name'])
        execution = Execution(**item.get('execution', {}))
        effective_config = replace(config, fee_bps=item.get('fee_bps', config.fee_bps))
        scenarios.append({'name': item['name'], 'config': asdict(effective_config), 'execution': asdict(execution)})
    locked = {'version': 1, 'instrument': plan['instrument'], 'datasets': datasets, 'scenarios': scenarios}
    return dict(locked, manifest_sha256=digest(locked))


def evaluate(manifest):
    locked = {key: value for key, value in manifest.items() if key != 'manifest_sha256'}
    if manifest['version'] != 1 or digest(locked) != manifest['manifest_sha256']:
        raise ValueError('Frozen manifest was changed or has an unsupported version')
    check_datasets(manifest['datasets'])
    # Verify all sources before computing any result, using the same bytes for parsing.
    sources = []
    for dataset in manifest['datasets']:
        raw = Path(dataset['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != dataset['sha256']:
            raise ValueError(f'Dataset changed since freeze: {dataset["name"]}')
        events = parse_csv(raw.decode('utf-8-sig'))
        if len(events) != dataset['event_count'] or events[0].timestamp_ms != dataset['start_ms'] or events[-1].timestamp_ms != dataset['end_ms']:
            raise ValueError('Dataset metadata disagrees with frozen input')
        sources.append((dataset, events))
    results = []
    for scenario in manifest['scenarios']:
        config = Config(**scenario['config'])
        execution = Execution(**scenario['execution'])
        for dataset, events in sources:
            comparison = compare(events, config, execution)
            results.append({'scenario': scenario['name'], 'dataset': dataset['name'], 'role': dataset['role'],
                            'event_count': len(events), 'duration_ms': dataset['end_ms'] - dataset['start_ms'],
                            **{strategy: {'metrics': comparison[strategy]['metrics'],
                                          'execution_diagnostics': comparison[strategy]['execution_diagnostics']}
                               for strategy in ('agent', 'baseline')},
                            'comparison': comparison['comparison']})
    summaries = []
    for scenario in manifest['scenarios']:
        for role in ROLES:
            rows = [r for r in results if r['scenario'] == scenario['name'] and r['role'] == role]
            if not rows:
                continue
            summary = {'scenario': scenario['name'], 'role': role, 'independent_segments': len(rows)}
            for strategy in ('agent', 'baseline'):
                metrics = [r[strategy]['metrics'] for r in rows]
                pnl = [m['net_pnl'] for m in metrics]
                summary[strategy] = {'mean_segment_pnl': sum(pnl) / len(pnl),
                                     'min_segment_pnl': min(pnl), 'max_segment_pnl': max(pnl),
                                     'worst_segment_drawdown': max(m['max_drawdown'] for m in metrics),
                                     'total_fill_count': sum(m['fill_count'] for m in metrics)}
            summaries.append(summary)
    return {'report_version': 1, 'model_version': 'passive-trade-replay-v1',
            'python_version': platform.python_version(), 'manifest_sha256': manifest['manifest_sha256'],
            'manifest': manifest, 'roles_present': sorted({d['role'] for d in manifest['datasets']}),
            'results': results, 'summaries': summaries,
            'assumptions': [
                'Every dataset starts with a separate account and no inventory; no fills or capital cross segment boundaries.',
                'Sensitivity scenarios change only specified execution assumptions and fees, never select or deploy a winner.',
                'Per-role means weight independent segments equally; they are not portfolio returns or compounded P&L.',
                'All datasets must describe the declared single instrument using a consistent epoch timestamp base.',
                'CSV replay uses event-time decisions; it does not reproduce live poll receipt times or user controls.',
                'Dataset hashes and frozen plans provide reproducibility, not evidence that holdout data were never viewed.',
                'Execution assumptions are illustrative; this study does not calibrate them against actual exchange fills.',
            ]}


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as target:
        target.write(json.dumps(value, indent=2, allow_nan=False) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('freeze', 'run'):
        sub = commands.add_parser(command)
        sub.add_argument('input', type=Path)
        sub.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = freeze(args.input) if args.command == 'freeze' else evaluate(json.loads(args.input.read_text()))
        write_new(args.output, result)
        print(json.dumps({'output': str(args.output), 'manifest_sha256': result['manifest_sha256']}, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Evaluation failed: {error}\n')


if __name__ == '__main__':
    main()
