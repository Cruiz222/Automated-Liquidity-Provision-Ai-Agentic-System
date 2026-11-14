"""Atomic paper-run batches, with CSV exports separated at feed discontinuities."""
import csv
import json
from pathlib import Path
import sqlite3

from .market_data import COLUMNS


class PaperStore:
    def __init__(self, directory, metadata):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.db = sqlite3.connect(self.directory / 'paper.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        with self.db:
            self.db.execute('CREATE TABLE metadata (payload TEXT NOT NULL)')
            self.db.execute('INSERT INTO metadata VALUES (?)', (json.dumps(metadata, allow_nan=False),))
            self.db.execute('CREATE TABLE batches (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.sequence = 0

    def append(self, payload):
        with self.db:
            self.db.execute('INSERT INTO batches VALUES (?, ?)',
                            (self.sequence + 1, json.dumps(payload, allow_nan=False)))
        self.sequence += 1

    def close(self):
        self.db.close()


def export_csv(journal, directory):
    """Never merge rows across a gap; each output begins with a book-only event."""
    target = Path(directory)
    uri = Path(journal).resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True)
    handles = {}
    counts = {}
    try:
        db.execute('BEGIN')
        metadata = json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0])
        target.mkdir(parents=True, exist_ok=False)
        with (target / 'metadata.json').open('x') as destination:
            json.dump(metadata, destination, indent=2)
        for payload, in db.execute('SELECT payload FROM batches ORDER BY sequence'):
            batch = json.loads(payload)
            for item in batch['events']:
                segment = item['segment']
                if segment not in handles:
                    handle = (target / f'market-{segment:04d}.csv').open('x', newline='')
                    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
                    writer.writeheader()
                    handles[segment] = (handle, writer)
                    counts[segment] = 0
                handles[segment][1].writerow(item['market'])
                counts[segment] += 1
        with (target / 'manifest.json').open('x') as destination:
            json.dump({'segments': counts, 'note': 'Segments are separate continuity windows; do not concatenate.'}, destination, indent=2)
        return counts
    finally:
        for handle, _ in handles.values():
            handle.close()
        db.close()
