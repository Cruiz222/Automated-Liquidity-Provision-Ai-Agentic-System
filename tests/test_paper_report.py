from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from app.backtest import Execution
from app.engine import Config
from app.paper import PaperRunner
from app.paper_report import audit
from app.paper_store import PaperStore


def poll(t, trades):
    return {'book': {'code': '0', 'data': [{'ts': str(t), 'bids': [['98', '10']], 'asks': [['102', '10']]}]},
            'trades': {'code': '0', 'data': [{'instId': 'BTC-USDT', 'tradeId': str(i), 'ts': str(ts), 'side': side, 'px': str(price), 'sz': str(size)} for i, ts, side, price, size in trades]}}


class PaperReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config = Config(half_spread_bps=100, inventory_skew_bps=0)
        execution = Execution(0, 1)
        self.store = PaperStore(self.root / 'run', {'version': 1, 'instrument': 'BTC-USDT', 'config': asdict(config), 'execution': asdict(execution)})
        self.now = 1000
        self.runner = PaperRunner(self.store, config=config, execution=execution, clock=lambda: self.now)
        self.addCleanup(self.runner.close)
        self.path = self.root / 'run' / 'paper.sqlite3'
        old = (1, 999, 'sell', 99, 1)
        first = (2, 1100, 'sell', 99, 1)
        self.runner.ingest(poll(1000, [old]))
        self.now = 1200
        self.runner.ingest(poll(1200, [old, first]))
        self.now = 1400
        self.runner.ingest(poll(1400, [first, (3, 1300, 'sell', 100, 1)]))

    def test_balances_outcomes_and_source_hash_are_reproducible(self):
        report = audit(self.path)
        self.assertTrue(report['accounting_verified'])
        self.assertEqual(report['fill_count'], 1)
        self.assertEqual(report['outcomes'], {'filled': 1, 'price_not_reached': 1})
        self.assertEqual(report['classification_sources'], {'recorded': 2})
        self.assertEqual(report, audit(self.path))
        self.assertAlmostEqual(report['final_account']['inventory'], 1)

    def test_legacy_records_are_labelled_as_inferred(self):
        rows = self.store.db.execute('SELECT sequence,payload FROM batches').fetchall()
        with self.store.db:
            for sequence, payload in rows:
                batch = json.loads(payload)
                for row in batch['events']:
                    row.pop('execution', None)
                self.store.db.execute('UPDATE batches SET payload=? WHERE sequence=?', (json.dumps(batch), sequence))
        report = audit(self.path)
        self.assertEqual(report['classification_sources'], {'inferred_from_saved_state': 2})
        self.assertEqual(report['outcomes']['filled'], 1)

    def test_inconsistent_balance_and_batch_gap_are_rejected(self):
        payload = json.loads(self.store.db.execute('SELECT payload FROM batches WHERE sequence=2').fetchone()[0])
        payload['events'][0]['account']['cash'] += 1
        with self.store.db:
            self.store.db.execute('UPDATE batches SET payload=? WHERE sequence=2', (json.dumps(payload),))
        with self.assertRaisesRegex(ValueError, 'Account mismatch'):
            audit(self.path)
        with self.store.db:
            self.store.db.execute('DELETE FROM batches WHERE sequence=2')
        with self.assertRaisesRegex(ValueError, 'sequence gap'):
            audit(self.path)

    def test_cli_output_and_file_protection(self):
        output = self.root / 'report.json'
        command = [sys.executable, '-m', 'app.paper_report', str(self.path), '--output', str(output)]
        subprocess.run(command, capture_output=True, check=True)
        before = output.read_bytes()
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(output.read_bytes(), before)
        self.assertIn('Paper audit failed', failed.stderr)

    def test_missing_journal_is_not_created(self):
        missing = self.root / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            audit(missing)
        self.assertFalse(missing.exists())
