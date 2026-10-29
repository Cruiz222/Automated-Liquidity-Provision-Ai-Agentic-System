"""Attribute simulated fill costs and future-mid markouts on development data."""
import argparse
from bisect import bisect_left
import hashlib
import json
import math
from pathlib import Path

from .backtest import Execution, compare
from .engine import Config
from .evaluate import verify_inputs, write_new
from .experiments import implementation_hash


DEFAULT_HORIZONS = (1000, 5000, 30000)


def validate(horizons, max_delay_ms):
    horizons = list(horizons)
    if not 1 <= len(horizons) <= 16 or any(type(h) is not int or h <= 0 for h in horizons):
        raise ValueError('Provide 1 to 16 positive integer markout horizons in milliseconds')
    if len(set(horizons)) != len(horizons):
        raise ValueError('Markout horizons must be unique')
    if type(max_delay_ms) is not int or max_delay_ms < 0:
        raise ValueError('max_delay_ms must be a nonnegative integer')
    return horizons


def attribute(events, replay, horizons=DEFAULT_HORIZONS, max_delay_ms=1000):
    """Markouts are diagnostics; only terminal contributions reconcile to P&L."""
    horizons = validate(horizons, max_delay_ms)
    times = [e.timestamp_ms for e in events]
    rows = []
    spread, movement, fees = [], [], []
    for fill in replay['fills']:
        index = fill['event']
        mid = events[index].mid
        signed_size = fill['size'] * (1 if fill['side'] == 'buy' else -1)
        capture = signed_size * (mid - fill['price'])
        terminal = signed_size * (events[-1].mid - mid)
        spread.append(capture)
        movement.append(terminal)
        fees.append(fill['fee'])
        markouts = []
        for horizon in horizons:
            target = fill['timestamp_ms'] + horizon
            next_index = bisect_left(times, target, lo=index + 1)
            item = {'horizon_ms': horizon, 'target_ms': target}
            if next_index == len(events):
                item.update(status='end_of_segment', observation_ms=None, delay_ms=None,
                            price_movement_pnl=None, gross_markout=None, net_markout=None)
            elif times[next_index] - target > max_delay_ms:
                item.update(status='observation_too_late', observation_ms=times[next_index],
                            delay_ms=times[next_index] - target,
                            price_movement_pnl=None, gross_markout=None, net_markout=None)
            else:
                future_mid = events[next_index].mid
                gross = signed_size * (future_mid - fill['price'])
                item.update(status='observed', observation_ms=times[next_index],
                            delay_ms=times[next_index] - target,
                            price_movement_pnl=signed_size * (future_mid - mid),
                            gross_markout=gross, net_markout=gross - fill['fee'])
            markouts.append(item)
        rows.append({**fill, 'fill_mid': mid, 'spread_capture': capture,
                     'terminal_mid_movement': terminal, 'markouts': markouts})
    spread_total, movement_total, fee_total = map(math.fsum, (spread, movement, fees))
    reconstructed = math.fsum((spread_total, movement_total, -fee_total))
    metrics = replay['metrics']
    residual = metrics['net_pnl'] - reconstructed
    tolerance = 1e-7 * max(1, len(rows))
    if len(rows) != metrics['fill_count'] or not math.isclose(fee_total, metrics['fees'], abs_tol=1e-9):
        raise ValueError('Fill history is incomplete or fees disagree with replay accounting')
    if abs(residual) > tolerance:
        raise ValueError('Fill contributions do not reconcile with replay P&L')
    summaries = []
    for horizon in horizons:
        selected = [(row, next(m for m in row['markouts'] if m['horizon_ms'] == horizon)) for row in rows]
        observed = [(row, m) for row, m in selected if m['status'] == 'observed']
        quantity = math.fsum(row['size'] for row, _ in observed)
        notional = math.fsum(row['price'] * row['size'] for row, _ in observed)
        gross = math.fsum(m['gross_markout'] for _, m in observed)
        net = math.fsum(m['net_markout'] for _, m in observed)
        summaries.append({'horizon_ms': horizon, 'observed_fill_count': len(observed),
                          'end_of_segment_count': sum(m['status'] == 'end_of_segment' for _, m in selected),
                          'observation_too_late_count': sum(m['status'] == 'observation_too_late' for _, m in selected),
                          'observed_quantity': quantity, 'observed_notional': notional,
                          'price_movement_pnl': math.fsum(m['price_movement_pnl'] for _, m in observed),
                          'gross_markout': gross, 'net_markout': net,
                          'net_markout_bps': net / notional * 10000 if notional else None})
    return {'metrics': metrics, 'attribution': {'spread_capture': spread_total,
            'terminal_mid_movement': movement_total, 'fees': fee_total,
            'reconstructed_net_pnl': reconstructed, 'accounting_residual': residual},
            'markout_summaries': summaries, 'fills': rows}


def study(manifest, horizons=DEFAULT_HORIZONS, max_delay_ms=1000):
    horizons = validate(horizons, max_delay_ms)
    sources = verify_inputs(manifest)
    sources = [(dataset, events) for dataset, events in sources if dataset['role'] == 'development']
    if not sources:
        raise ValueError('Fill-cost studies require development datasets')
    results = []
    for scenario in manifest['scenarios']:
        for dataset, events in sources:
            replay = compare(events, Config(**scenario['config']), Execution(**scenario['execution']))
            results.append({'scenario': scenario['name'], 'dataset': dataset['name'], 'role': 'development',
                            **{strategy: attribute(events, replay[strategy], horizons, max_delay_ms)
                               for strategy in ('agent', 'baseline')}})
    return {'report_version': 1, 'model_version': 'development-fill-cost-v1',
            'manifest_sha256': manifest['manifest_sha256'], 'manifest': manifest,
            'implementation_sha256': implementation_hash(),
            'attribution_implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'horizons_ms': horizons, 'max_delay_ms': max_delay_ms, 'results': results,
            'assumptions': [
                'All fills are simulated; only development datasets are evaluated. No setting is selected or deployed.',
                'Spread capture is signed fill quantity times fill-event midpoint minus execution price.',
                'Terminal midpoint movement is signed fill quantity times final midpoint minus fill-event midpoint.',
                'Spread capture plus terminal midpoint movement minus fees reconciles to marked account P&L.',
                'Positive price-movement contributions are favorable; negative values indicate adverse movement.',
                'Each markout uses the first row at or after its target within the same dataset; no interpolation or cross-segment prices.',
                'Missing or delayed observations are excluded and counted, never substituted with zero.',
                'Markout sums overlap exposures and are diagnostics, not additive portfolio returns or realized P&L.',
                'Net markout subtracts the entry fill fee only, without hypothetical exit fees or slippage.',
                'Results inherit the replay execution assumptions and do not establish actual exchange execution costs.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--horizons-ms', type=int, nargs='+', default=DEFAULT_HORIZONS)
    parser.add_argument('--max-delay-ms', type=int, default=1000)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise FileExistsError(f'Output already exists: {args.output}')
        report = study(json.loads(args.manifest.read_text()), args.horizons_ms, args.max_delay_ms)
        write_new(args.output, report)
        print(json.dumps({'output': str(args.output), 'results': [
            {'scenario': row['scenario'], 'dataset': row['dataset'],
             'agent': row['agent']['attribution'], 'markouts': row['agent']['markout_summaries']}
            for row in report['results']]}, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Fill-cost study failed: {error}\n')


if __name__ == '__main__':
    main()
