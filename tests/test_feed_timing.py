from pathlib import Path
import sqlite3
import tempfile
import unittest
from app.feed_timing import analyze
from app.paper_store import PaperStore


class FeedTimingTests(unittest.TestCase):
    def test_silent_trade_watermark_is_distinguished_from_book_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PaperStore(Path(directory) / 'run', {})
            store.append({'received_ms': 1000, 'raw': {}, 'events': [{'segment': 1, 'market': {'timestamp_ms': 900}}],
                          'health': {'book': {'timestamp_ms': 900}, 'anchor': 1}})
            store.append({'received_ms': 8000, 'raw': {}, 'events': [],
                          'health': {'book': {'timestamp_ms': 7900}, 'anchor': 1}})
            store.append({'received_ms': 9000, 'raw': {}, 'events': [{'segment': 1, 'market': {'timestamp_ms': 8800}}],
                          'health': {'book': {'timestamp_ms': 8900}, 'anchor': 2}})
            store.close()
            report = analyze(Path(directory) / 'run' / 'paper.sqlite3')
            self.assertEqual(report['fresh_book_batches_waiting_for_trade_watermark'], 1)
            self.assertEqual(report['alignment_violations'][0]['alignment_lag_ms'], 7000)
            self.assertEqual(report['event_spacing_violations'][0]['gap_ms'], 7900)
            self.assertEqual(report['max_book_age_ms'], 100)

    def test_missing_journal_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite3'
            with self.assertRaises(sqlite3.OperationalError):
                analyze(path)
            self.assertFalse(path.exists())
