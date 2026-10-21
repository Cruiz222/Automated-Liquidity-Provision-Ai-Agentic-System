from dataclasses import asdict
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.backtest import Execution
from app.capture_review import Criteria, review
from app.engine import Config
from app.paper import PaperRunner, collect
from app.paper_store import PaperStore


def payload(t, trades):
    return {'book': {'code': '0', 'data': [{'ts': str(t), 'bids': [['98', '10']], 'asks': [['102', '10']]}]},
            'trades': {'code': '0', 'data': [{'instId': 'BTC-USDT', 'tradeId': str(i), 'ts': str(ts), 'px': '100', 'sz': '1', 'side': 'sell'} for i, ts in trades]}}


class CaptureReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config, execution = Config(), Execution()
        self.store = PaperStore(self.root / 'run', {'version': 1, 'instrument': 'BTC-USDT', 'config': asdict(config), 'execution': asdict(execution)})
        self.now = 1000
        self.runner = PaperRunner(self.store, config=config, execution=execution, clock=lambda: self.now)
        self.addCleanup(self.runner.close)
        self.path = self.root / 'run' / 'paper.sqlite3'

    def populate(self):
        self.runner.ingest(payload(1000, [(1, 999)]))
        self.now = 2000
        self.runner.ingest(payload(2000, [(1, 999), (2, 1900)]))

    def test_defaults_reject_short_run_without_rejecting_valid_accounting(self):
        self.populate()
        report = review(self.path)
        self.assertEqual(report['status'], 'more_data_needed')
        self.assertTrue(report['accounting_verified'])
        self.assertIn('duration', report['segments'][0]['failed_checks'])
        self.assertIn('trade_count', report['segments'][0]['failed_checks'])

    def test_explicit_criteria_qualify_segment_and_preserve_zero_fill_distinction(self):
        self.populate()
        report = review(self.path, Criteria(min_duration_ms=800, min_trades=1, min_healthy_poll_fraction=.5))
        self.assertEqual(report['qualified_segments'], ['1'])
        self.assertEqual(report['simulated_fill_count'], 0)
        self.assertEqual(report['segments'][0]['max_alignment_lag_ms'], 100)

    def test_fault_polls_reduce_healthy_fraction(self):
        self.populate()
        self.runner.fail('offline')
        report = review(self.path, Criteria(min_duration_ms=0, min_trades=1, min_healthy_poll_fraction=.5))
        self.assertEqual(report['polls'], 3)
        self.assertAlmostEqual(report['segments'][0]['healthy_poll_fraction'], 1/3)
        self.assertIn('feed_health', report['segments'][0]['failed_checks'])

    def test_qualification_exit_codes(self):
        self.populate()
        command = [sys.executable, '-m', 'app.capture_review', str(self.path), '--require-qualified']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        result = subprocess.run(command + ['--min-duration-seconds', '0', '--min-trades', '1',
                                          '--min-healthy-fraction', '.5'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['qualified_segments'], ['1'])

    def test_empty_run_and_invalid_thresholds(self):
        self.assertEqual(review(self.path)['segments'], [])
        for kwargs in ({'min_trades': -1}, {'min_duration_ms': .5}, {'min_healthy_poll_fraction': float('nan')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Criteria(**kwargs)


class CollectionTimingTests(unittest.TestCase):
    def run_collection(self, polls=0, duration=None, accepted=True):
        now = [0.0]
        runner = type('Runner', (), {'snapshot': lambda _: {'feed': {}}})()
        def sleep(seconds):
            now[0] += seconds
        with patch('app.paper.poll_once', return_value=accepted) as poll:
            result = collect(runner, object(), polls=polls, duration_seconds=duration,
                             clock=lambda: now[0], sleep=sleep, emit=lambda value: None)
        return result, poll.call_count

    def test_duration_caps_sleep_and_poll_limit_stops_first(self):
        result, calls = self.run_collection(duration=2.5)
        self.assertEqual(calls, 3)
        self.assertEqual(result['elapsed_seconds'], 2.5)
        result, calls = self.run_collection(polls=2, duration=10)
        self.assertEqual(calls, 2)
        self.assertEqual(result['elapsed_seconds'], 1)

    def test_failure_backoff_is_clipped_and_invalid_duration_rejected(self):
        result, calls = self.run_collection(duration=.5, accepted=False)
        self.assertEqual(calls, 1)
        self.assertEqual(result['successful_polls'], 0)
        self.assertEqual(result['elapsed_seconds'], .5)
        for duration in (0, -1, float('nan')):
            with self.assertRaises(ValueError):
                self.run_collection(duration=duration)
