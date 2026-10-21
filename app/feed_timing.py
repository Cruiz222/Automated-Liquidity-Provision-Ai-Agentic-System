"""Read-only timing evidence for REST watermarks and streamed paper captures."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def analyze(path, limit_ms=5000):
    if limit_ms <= 0:
        raise ValueError('limit_ms must be positive')
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    previous = previous_receipt = None
    digest = hashlib.sha256()
    gaps, lags, book_ages, receipt_gaps = [], [], [], []
    batches = trade_watermark_stalls = 0
    try:
        db.execute('BEGIN')
        for sequence, payload in db.execute('SELECT sequence,payload FROM batches ORDER BY sequence'):
            if sequence != batches + 1:
                raise ValueError('Batch sequence gap')
            batches += 1
            digest.update((str(sequence) + ':' + payload + '\n').encode())
            batch = json.loads(payload)
            received = batch['received_ms']
            if previous_receipt is not None:
                receipt_gaps.append(received - previous_receipt)
            previous_receipt = received
            for row in batch['events']:
                event_ms = row['market']['timestamp_ms']
                if previous is not None and row['segment'] == previous[0]:
                    spacing = event_ms - previous[1]
                    if spacing > limit_ms:
                        gaps.append({'batch': sequence, 'segment': row['segment'], 'gap_ms': spacing})
                previous = (row['segment'], event_ms)
            book = batch['health'].get('book')
            if book is not None:
                age = received - book['timestamp_ms']
                book_ages.append(age)
                if previous is not None:
                    lag = max(0, book['timestamp_ms'] - previous[1])
                    if lag > limit_ms:
                        no_rows = not batch['events']
                        trade_watermark_stalls += int(no_rows and 0 <= age <= limit_ms and (batch.get('raw') or {}).get('transport') != 'websocket')
                        lags.append({'batch': sequence, 'alignment_lag_ms': lag,
                                     'book_age_ms': age, 'no_processed_rows': no_rows,
                                     'trade_anchor': batch['health'].get('anchor')})
        return {'report_version': 1, 'journal': str(path), 'committed_batches_sha256': digest.hexdigest(),
                'limit_ms': limit_ms, 'batches': batches, 'event_spacing_violations': gaps,
                'alignment_violations': lags, 'fresh_book_batches_waiting_for_trade_watermark': trade_watermark_stalls,
                'max_poll_receipt_gap_ms': max(receipt_gaps, default=None),
                'max_book_age_ms': max(book_ages, default=None),
                'interpretation': 'For REST captures, fresh books with no processed rows identify watermark waiting. They cannot distinguish trade inactivity from cached or delayed trade responses. No missing-trade claim follows from these timestamps alone.'}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('--limit-ms', type=int, default=5000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = analyze(args.journal, args.limit_ms)
        text = json.dumps(report, indent=2, allow_nan=False) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x') as target:
                target.write(text)
            print(f'Timing report: {args.output}')
        else:
            print(text, end='')
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Timing analysis failed: {error}\n')


if __name__ == '__main__':
    main()
