"""Review completed paper captures against explicit research-data criteria."""
import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .market_data import MarketEvent
from .paper_report import audit


@dataclass(frozen=True)
class Criteria:
    min_duration_ms: int = 300_000
    min_trades: int = 1000
    max_event_gap_ms: int = 5000
    max_alignment_lag_ms: int = 5000
    min_healthy_poll_fraction: float = .9

    def __post_init__(self):
        for key in ('min_duration_ms', 'min_trades', 'max_event_gap_ms', 'max_alignment_lag_ms'):
            value = getattr(self, key)
            if type(value) is not int or value < 0:
                raise ValueError(f'{key} must be a nonnegative integer')
        if not math.isfinite(self.min_healthy_poll_fraction) or not 0 <= self.min_healthy_poll_fraction <= 1:
            raise ValueError('min_healthy_poll_fraction must be between zero and one')


def review(path, criteria=None):
    criteria = criteria or Criteria()
    accounting = audit(path)
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    segments = {}
    digest = hashlib.sha256()
    polls = healthy_polls = 0
    current_segment = None
    try:
        db.execute('BEGIN')
        for sequence, payload in db.execute('SELECT sequence,payload FROM batches ORDER BY sequence'):
            digest.update((str(sequence) + ':' + payload + '\n').encode())
            batch = json.loads(payload)
            for row in batch['events']:
                event = MarketEvent(**row['market'])
                current_segment = str(row['segment'])
                stats = segments.setdefault(current_segment, {'segment': current_segment, 'events': 0, 'trades': 0,
                    'start_ms': event.timestamp_ms, 'end_ms': event.timestamp_ms,
                    'max_event_gap_ms': 0, 'polls': 0, 'healthy_polls': 0, 'max_alignment_lag_ms': 0,
                    'alignment_observed': False, 'bootstrap_alignment_polls_excluded': 0, 'starts_with_book': event.trade_side is None})
                if event.timestamp_ms < stats['end_ms']:
                    raise ValueError('Segment event timestamps regressed')
                stats['max_event_gap_ms'] = max(stats['max_event_gap_ms'], event.timestamp_ms - stats['end_ms'])
                stats['end_ms'] = event.timestamp_ms
                stats['events'] += 1
                stats['trades'] += int(event.trade_side is not None)
            if batch.get('raw') is not None or batch.get('control') == 'feed_fault':
                polls += 1
                healthy = batch['health'].get('status') == 'healthy'
                healthy_polls += int(healthy)
                if current_segment is not None:
                    stats = segments[current_segment]
                    stats['polls'] += 1
                    stats['healthy_polls'] += int(healthy)
                    health = batch['health']
                    if health.get('book') and 'processed_book' in health and health['processed_book'] is None:
                        stats['bootstrap_alignment_polls_excluded'] += 1
                    elif health.get('book'):
                        lag = max(0, health['book']['timestamp_ms'] - stats['end_ms'])
                        stats['max_alignment_lag_ms'] = max(stats['max_alignment_lag_ms'], lag)
                        stats['alignment_observed'] = True
        if digest.hexdigest() != accounting['source']['committed_batches_sha256']:
            raise ValueError('Journal changed during review; stop capture or retry')
    finally:
        db.close()
    qualified = []
    for segment, stats in segments.items():
        stats['duration_ms'] = stats['end_ms'] - stats['start_ms']
        stats['healthy_poll_fraction'] = stats['healthy_polls'] / stats['polls'] if stats['polls'] else 0
        checks = {'at_least_two_events': stats['events'] >= 2,
                  'starts_with_book': stats['starts_with_book'],
                  'duration': stats['duration_ms'] >= criteria.min_duration_ms,
                  'trade_count': stats['trades'] >= criteria.min_trades,
                  'event_spacing': stats['max_event_gap_ms'] <= criteria.max_event_gap_ms,
                  'feed_health': stats['healthy_poll_fraction'] >= criteria.min_healthy_poll_fraction,
                  'alignment_observed': stats['alignment_observed'],
                  'alignment_lag': stats['max_alignment_lag_ms'] <= criteria.max_alignment_lag_ms}
        stats['checks'] = checks
        stats['failed_checks'] = [name for name, passed in checks.items() if not passed]
        stats['qualified'] = all(checks.values())
        if stats['qualified']:
            qualified.append(segment)
    return {'report_version': 1, 'review_model': 'segment-alignment-v2', 'criteria': asdict(criteria), 'source': accounting['source'],
            'instrument': accounting['instrument'], 'polls': polls, 'healthy_polls': healthy_polls,
            'segments': list(segments.values()), 'qualified_segments': qualified,
            'status': 'qualified_segments_available' if qualified else 'more_data_needed',
            'accounting_verified': accounting['accounting_verified'],
            'simulated_fill_count': accounting['fill_count'], 'final_account': accounting['final_account'],
            'notes': ['Criteria are configurable research-data policy, not statistically calibrated thresholds.',
                      'Segments remain separate; qualifying one does not certify the whole run.',
                      'WebSocket alignment is undefined before a processed snapshot; bootstrap polls count toward health but are not compared with an older segment clock.',
                      'Event spacing and overlap checks cannot establish feed completeness.',
                      'Simulated fills do not calibrate real exchange queue position or establish profitability.',
                      'Already inspected captures are not untouched holdout data.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--min-duration-seconds', type=int, default=300)
    parser.add_argument('--min-trades', type=int, default=1000)
    parser.add_argument('--max-event-gap-ms', type=int, default=5000)
    parser.add_argument('--max-alignment-lag-ms', type=int, default=5000)
    parser.add_argument('--min-healthy-fraction', type=float, default=.9)
    parser.add_argument('--require-qualified', action='store_true')
    args = parser.parse_args()
    try:
        criteria = Criteria(args.min_duration_seconds * 1000, args.min_trades, args.max_event_gap_ms,
                            args.max_alignment_lag_ms, args.min_healthy_fraction)
        report = review(args.journal, criteria)
        text = json.dumps(report, indent=2, allow_nan=False) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x') as target:
                target.write(text)
            print(f'Capture review: {report["status"]}; report: {args.output}')
        else:
            print(text, end='')
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Capture review failed: {error}\n')
    if args.require_qualified and not report['qualified_segments']:
        parser.exit(2)


if __name__ == '__main__':
    main()
