from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.backtest import Execution
from app.capture_review import Criteria
from app.engine import Config
from app.experiments import read_runs
from app.paper import PaperRunner
from app.paper_store import PaperStore
from app.pipeline import run_pipeline


def payload(t, trades):
    return {'book': {'code': '0', 'data': [{'ts': str(t), 'bids': [['98', '10']], 'asks': [['102', '10']]}]},
            'trades': {'code': '0', 'data': [{'instId': 'BTC-USDT', 'tradeId': str(i), 'ts': str(ts), 'px': '100', 'sz': '1', 'side': 'sell'} for i, ts in trades]}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / 'workflow'
        self.registry = self.root / 'registry.sqlite3'
        config, execution = Config(), Execution()
        store = PaperStore(self.root / 'capture', {'version': 1, 'instrument': 'BTC-USDT',
                           'config': asdict(config), 'execution': asdict(execution)})
        now = [1000]
        runner = PaperRunner(store, config=config, execution=execution, clock=lambda: now[0])
        runner.ingest(payload(1000, [(1, 999)]))
        now[0] = 2000
        runner.ingest(payload(2000, [(1, 999), (2, 1900)]))
        runner.close()
        self.journal = self.root / 'capture' / 'paper.sqlite3'
        self.criteria = Criteria(min_duration_ms=0, min_trades=1, min_healthy_poll_fraction=.5)

    def test_qualified_capture_runs_complete_development_workflow(self):
        state = run_pipeline(self.journal, self.workspace, self.registry, self.criteria)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state, json.loads((self.workspace / 'pipeline.json').read_text()))
        run = read_runs(self.registry, state['experiment_id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['summary']['roles_present'], ['development'])
        self.assertEqual(run['summary']['results'], 4)
        self.assertEqual([step['phase'] for step in state['history']], [
            'reviewing_capture', 'exporting_segments', 'freezing_reference', 'generating_offline_proposal',
            'materializing_study', 'evaluating_study', 'finished'])
        self.assertTrue(Path(state['artifacts']['experiment_report']).exists())

    def test_short_capture_stops_before_export_or_evaluation(self):
        state = run_pipeline(self.journal, self.workspace, self.registry)
        self.assertEqual(state['status'], 'more_data_needed')
        self.assertIsNone(state['experiment_id'])
        self.assertFalse((self.workspace / 'market').exists())
        self.assertFalse(self.registry.exists())
        self.assertTrue((self.workspace / 'capture-review.json').exists())

    def test_failure_is_checkpointed_and_workspace_is_never_overwritten(self):
        with patch('app.pipeline.freeze', side_effect=ValueError('cannot freeze')):
            with self.assertRaisesRegex(ValueError, 'cannot freeze'):
                run_pipeline(self.journal, self.workspace, self.registry, self.criteria)
        state = json.loads((self.workspace / 'pipeline.json').read_text())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['phase'], 'freezing_reference')
        self.assertIn('cannot freeze', state['error'])
        before = (self.workspace / 'pipeline.json').read_bytes()
        with self.assertRaises(FileExistsError):
            run_pipeline(self.journal, self.workspace, self.registry)
        self.assertEqual(before, (self.workspace / 'pipeline.json').read_bytes())

    def test_changed_capture_after_export_is_rejected(self):
        from app.pipeline import review
        original = review(self.journal, self.criteria)
        changed = json.loads(json.dumps(original))
        changed['source']['committed_batches_sha256'] = 'changed'
        with patch('app.pipeline.review', side_effect=[original, changed]):
            with self.assertRaisesRegex(ValueError, 'Capture changed'):
                run_pipeline(self.journal, self.workspace, self.registry, self.criteria)
        self.assertFalse(self.registry.exists())

    def test_missing_journal_fails_before_creating_workspace(self):
        with self.assertRaisesRegex(ValueError, 'Capture journal not found'):
            run_pipeline(self.root / 'missing.sqlite3', self.workspace, self.registry)
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.registry.exists())

    def test_cli_more_data_exit_status_and_no_network(self):
        command = [sys.executable, '-m', 'app.pipeline', str(self.journal), '--output-dir', str(self.workspace), '--registry', str(self.registry)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('more_data_needed', result.stdout)
        self.assertFalse(self.registry.exists())
