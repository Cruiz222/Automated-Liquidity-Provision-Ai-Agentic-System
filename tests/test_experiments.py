import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.evaluate import freeze, write_new
from app.experiments import Registry, read_runs, run_study


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.registry = self.root / 'registry.sqlite3'
        self.csv = self.root / 'sample.csv'
        self.csv.write_bytes((Path(__file__).resolve().parents[1] / 'examples/market_sample.csv').read_bytes())
        plan = {'version': 1, 'instrument': 'SYNTHETIC-USD', 'datasets': [
            {'name': 'sample', 'path': 'sample.csv', 'role': 'development'}], 'scenarios': [{'name': 'default'}]}
        write_new(self.root / 'plan.json', plan)
        self.manifest = freeze(self.root / 'plan.json')
        self.path = self.root / 'manifest.json'
        write_new(self.path, self.manifest)

    def run_study(self, repeat=False):
        return run_study(self.path, self.registry, self.root / 'results', repeat)

    def test_completion_cache_and_explicit_repeat(self):
        first = self.run_study()
        self.assertEqual(first['status'], 'completed')
        with patch('app.experiments.evaluate', side_effect=AssertionError('Cached result must skip evaluation')):
            cached = self.run_study()
        self.assertTrue(cached['reused'])
        self.assertEqual(first['id'], cached['id'])
        repeated = self.run_study(True)
        self.assertFalse(repeated['reused'])
        self.assertNotEqual(first['id'], repeated['id'])
        self.assertTrue(Path(first['result_path']).exists())
        self.assertEqual(len(read_runs(self.registry)), 2)

    def test_changed_inputs_are_checked_before_cache_reuse(self):
        self.run_study()
        self.csv.write_text('modified input')
        with self.assertRaisesRegex(ValueError, 'Dataset changed'):
            self.run_study()
        self.assertEqual(read_runs(self.registry)[0]['status'], 'failed')

    def test_changed_result_is_not_reused(self):
        run = self.run_study()
        Path(run['result_path']).write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            self.run_study()
        self.assertNotEqual(run['id'], self.run_study(True)['id'])

    def test_implementation_fingerprint_invalidates_reuse(self):
        first = self.run_study()
        with patch('app.experiments.implementation_hash', return_value='changed-implementation'):
            second = self.run_study()
        self.assertNotEqual(first['id'], second['id'])
        self.assertFalse(second['reused'])

    def test_failure_is_recorded_without_a_completed_report(self):
        with patch('app.experiments.evaluate', side_effect=RuntimeError('evaluation failed')):
            with self.assertRaisesRegex(RuntimeError, 'evaluation failed'):
                self.run_study()
        run = read_runs(self.registry)[0]
        self.assertEqual(run['status'], 'failed')
        self.assertIsNone(run['result_path'])
        self.assertIn('evaluation failed', run['error'])

    def test_concurrent_reservations_and_terminal_states(self):
        one, two = Registry(self.registry), Registry(self.registry)
        try:
            identifier = one.start('same', self.manifest, 'code')
            with self.assertRaisesRegex(ValueError, 'already marked running'):
                two.start('same', self.manifest, 'code')
            one.finish(identifier, 'interrupted', error='Worker already stopped')
            with self.assertRaisesRegex(ValueError, 'no longer running'):
                one.finish(identifier, 'completed')
            self.assertNotEqual(identifier, two.start('same', self.manifest, 'code'))
        finally:
            one.close()
            two.close()

    def test_missing_read_only_registry_does_not_create_file(self):
        self.assertEqual(read_runs(self.registry), [])
        self.assertFalse(self.registry.exists())
        self.assertIsNone(read_runs(self.registry, 'missing'))

    def request(self, path):
        from app import server
        class Socket:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, *args):
                return io.BytesIO(f'GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n'.encode())
            def sendall(self, value):
                self.output.extend(value)
        connection = Socket()
        with patch.object(server, 'registry_path', self.registry), patch.object(server.Handler, 'log_message'):
            server.Handler(connection, ('127.0.0.1', 1), SimpleNamespace())
        headers, body = bytes(connection.output).split(b'\r\n\r\n', 1)
        return headers, json.loads(body)

    def test_read_only_dashboard_listing_detail_and_unknown_id(self):
        headers, result = self.request('/api/experiments')
        self.assertIn(b'200', headers)
        self.assertEqual(result, [])
        self.assertFalse(self.registry.exists())
        run = self.run_study()
        _, rows = self.request('/api/experiments')
        self.assertEqual(rows[0]['id'], run['id'])
        _, detail = self.request('/api/experiments/' + run['id'])
        self.assertEqual(detail['status'], 'completed')
        headers, _ = self.request('/api/experiments/not-an-id')
        self.assertIn(b'404', headers)
