"""Run an offline quote-width sensitivity study on frozen development captures."""
import argparse
import copy
import json
import math
from pathlib import Path
import sqlite3
import uuid

from .evaluate import digest, verify_inputs, write_new
from .experiments import DEFAULT_REGISTRY, run_study


DEFAULT_WIDTHS = (0.01, 0.1, 1.0, 12.0)


def derive(manifest, widths=DEFAULT_WIDTHS, reference='reference'):
    """Verify all inputs, then vary only half spread on development datasets."""
    verify_inputs(manifest)
    widths = list(widths)
    if not 1 <= len(widths) <= 16 or any(
            isinstance(w, bool) or not isinstance(w, (int, float)) or
            not math.isfinite(w) or w < 0 for w in widths):
        raise ValueError('Provide 1 to 16 finite, nonnegative half spreads in basis points')
    if len(set(widths)) != len(widths):
        raise ValueError('Half spreads must be unique')
    matches = [s for s in manifest['scenarios'] if s['name'] == reference]
    if len(matches) != 1:
        raise ValueError('Select one uniquely named reference scenario with --reference')
    source = matches[0]
    datasets = [copy.deepcopy(d) for d in manifest['datasets'] if d['role'] == 'development']
    if not datasets:
        raise ValueError('Quote studies require development datasets')
    scenarios = [dict(copy.deepcopy(source), name='reference')]
    for width in widths:
        if width == source['config']['half_spread_bps']:
            continue
        scenario = copy.deepcopy(source)
        # Use round-trip precision for names, including arbitrarily close widths.
        scenario['name'] = f'half-spread-{float(width)!r}-bps'
        scenario['config']['half_spread_bps'] = width
        scenarios.append(scenario)
    locked = {'version': 1, 'instrument': manifest['instrument'], 'datasets': datasets,
              'scenarios': scenarios, 'quote_study_provenance': {
                  'model': 'development-quote-width-v1',
                  'base_manifest_sha256': manifest['manifest_sha256'],
                  'reference_scenario': reference,
                  'requested_half_spreads_bps': widths,
                  'purpose': 'Offline sensitivity only; no winner selection or live configuration changes.'}}
    return dict(locked, manifest_sha256=digest(locked))


def run(manifest_path, widths=DEFAULT_WIDTHS, reference='reference', workspace=None,
        registry=DEFAULT_REGISTRY):
    base = json.loads(Path(manifest_path).read_text())
    manifest = derive(base, widths, reference)
    folder = Path(workspace) if workspace else Path('data') / ('quote-study-' + uuid.uuid4().hex[:10])
    folder.mkdir(parents=True, exist_ok=False)
    write_new(folder / 'base.lock.json', base)
    path = folder / 'study.lock.json'
    write_new(path, manifest)
    experiment = run_study(path, registry, folder / 'experiments')
    report = json.loads(Path(experiment['result_path']).read_text())
    configs = {s['name']: s['config'] for s in manifest['scenarios']}
    rows = [{'scenario': row['scenario'], 'dataset': row['dataset'],
             'half_spread_bps': configs[row['scenario']]['half_spread_bps'],
             **{strategy: row[strategy]['metrics'] for strategy in ('agent', 'baseline')}}
            for row in report['results']]
    result = {'workspace': str(folder), 'experiment_id': experiment['id'],
              'report_path': experiment['result_path'], 'results': rows,
              'limitations': [
                  'Development data only; this study does not select or deploy a setting.',
                  'Replay fills are illustrative; tick sizes, lot sizes and actual exchange queue positions are not calibrated.',
                  'Net P&L includes the frozen fees and marks remaining inventory at the final midpoint.',
                  'Only half spread varies; account limits, inventory skew, fees and execution assumptions stay fixed.']}
    write_new(folder / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--half-spreads-bps', type=float, nargs='+', default=DEFAULT_WIDTHS)
    parser.add_argument('--reference', default='reference')
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    args = parser.parse_args()
    try:
        result = run(args.manifest, args.half_spreads_bps, args.reference, args.workspace, args.registry)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(1, f'Quote study failed: {error}\n')


if __name__ == '__main__':
    main()
