"""Audit saved paper balances and explain simulated fill outcomes, without network access."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sqlite3


def summary(values):
    return {'count': len(values), 'min': min(values) if values else None,
            'mean': sum(values) / len(values) if values else None,
            'max': max(values) if values else None}


def infer_execution(row):
    """Older journals lack pre-execution outcomes: avoid inferring queue or latency."""
    event = row['market']
    if not event.get('trade_side'):
        return {'outcome': 'no_trade'}
    if not row.get('running', True):
        return {'outcome': 'paused'}
    if row['fills']:
        return {'outcome': 'filled'}
    side = 'bid' if event['trade_side'] == 'sell' else 'ask'
    order = row['active_orders'].get(side)
    if order is None:
        return {'outcome': 'no_active_quote'}
    distance = (event['trade_price'] - order['price'] if side == 'bid' else order['price'] - event['trade_price']) / order['price'] * 10_000
    return {'outcome': 'price_not_reached' if distance > 0 else 'unfilled_reachable_quote',
            'distance_bps': max(0, distance)}


def audit(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        metadata = json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0])
        if metadata.get('version') != 1:
            raise ValueError('Unsupported paper journal version')
        config = metadata['config']
        cash, inventory, fees = config['initial_cash'], 0.0, 0.0
        counts, sources, faults = Counter(), Counter(), Counter()
        per_segment = {}
        spreads, distances = [], []
        digest = hashlib.sha256()
        batches = event_count = fill_count = successful_polls = 0
        final_health = final_account = None
        peak = cash
        max_drawdown = 0.0
        for sequence, payload in db.execute('SELECT sequence,payload FROM batches ORDER BY sequence'):
            if sequence != batches + 1:
                raise ValueError('Journal batch sequence gap')
            batches += 1
            digest.update((str(sequence) + ':' + payload + '\n').encode())
            batch = json.loads(payload)
            final_health = batch['health']
            successful_polls += int(batch.get('raw') is not None and batch.get('control') is None)
            if final_health.get('error'):
                faults[final_health['error']] += 1
            for row in batch['events']:
                event_count += 1
                market, account = row['market'], row['account']
                mid = (market['bid'] + market['ask']) / 2
                spreads.append((market['ask'] - market['bid']) / mid * 10_000)
                segment = str(row['segment'])
                totals = per_segment.setdefault(segment, {'events': 0, 'trades': 0, 'fills': 0, 'outcomes': {}})
                totals['events'] += 1
                if market.get('trade_side'):
                    source = 'recorded' if 'execution' in row else 'inferred_from_saved_state'
                    outcome = row.get('execution') or infer_execution(row)
                    sources[source] += 1
                    counts[outcome['outcome']] += 1
                    totals['trades'] += 1
                    totals['outcomes'][outcome['outcome']] = totals['outcomes'].get(outcome['outcome'], 0) + 1
                    if outcome['outcome'] == 'price_not_reached' and 'distance_bps' in outcome:
                        distances.append(outcome['distance_bps'])
                for fill in row['fills']:
                    price, size, fee = fill['price'], fill['size'], fill['fee']
                    if fill['side'] not in ('buy', 'sell') or not all(math.isfinite(v) for v in (price, size, fee)) or min(price, size) <= 0 or fee < 0:
                        raise ValueError('Invalid recorded fill')
                    expected_fee = price * size * config['fee_bps'] / 10_000
                    if not math.isclose(fee, expected_fee, abs_tol=1e-9, rel_tol=1e-12):
                        raise ValueError('Recorded fill fee disagrees with run configuration')
                    cash += (-price * size if fill['side'] == 'buy' else price * size) - fee
                    inventory += size if fill['side'] == 'buy' else -size
                    fees += fee
                    fill_count += 1
                    totals['fills'] += 1
                expected = {'cash': cash, 'inventory': inventory, 'fees': fees, 'equity': cash + inventory * mid}
                for key, value in expected.items():
                    if not math.isclose(account[key], value, rel_tol=1e-12, abs_tol=1e-7):
                        raise ValueError(f'Account mismatch at batch {sequence}, event {event_count}: {key}')
                if cash < -1e-7 or not -1e-7 <= inventory <= config['max_inventory'] + 1e-7:
                    raise ValueError('Recorded account violates cash or inventory limits')
                peak = max(peak, expected['equity'])
                max_drawdown = max(max_drawdown, peak - expected['equity'])
                final_account = dict(account)
        return {'report_version': 1, 'source': {'journal': str(path), 'committed_batches_sha256': digest.hexdigest()},
                'instrument': metadata.get('instrument'), 'config': config, 'execution': metadata.get('execution'),
                'batches': batches, 'successful_polls': successful_polls, 'events': event_count,
                'trade_events': sum(counts.values()), 'fill_count': fill_count,
                'outcomes': dict(counts), 'classification_sources': dict(sources),
                'market_spread_bps': summary(spreads), 'missed_price_distance_bps': summary(distances),
                'fault_occurrences': dict(faults), 'segments': per_segment, 'final_health': final_health,
                'accounting_verified': True, 'final_account': final_account,
                'net_pnl': final_account['equity'] - config['initial_cash'] if final_account else 0,
                'max_drawdown': max_drawdown,
                'limitations': [
                    'Historical classifications use post-execution saved orders and cannot identify latency or queue causes precisely.',
                    'Spread statistics weight processed rows equally; they are not time-weighted market estimates.',
                    'Only normalized accepted trade rows are classified; warm-up and gap-skipped trades are excluded.',
                    'Accounting verification checks saved fills and balances; it does not verify exchange fills or establish profitability.',
                    'Execution assumptions remain illustrative; this is a diagnostic audit, not calibration to real fills.',
                ]}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = audit(args.journal)
        text = json.dumps(report, indent=2, allow_nan=False) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x', encoding='utf-8') as destination:
                destination.write(text)
            print(f'Verified {report["events"]} events; report: {args.output}')
        else:
            print(text, end='')
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Paper audit failed: {error}\n')


if __name__ == '__main__':
    main()
