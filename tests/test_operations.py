import io
import json
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.operations import snapshot


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'data with spaces'
        self.registry = self.root / 'registry.sqlite3'

    def save_capture(self, qualified=False):
        folder = self.root / 'paper-one'
        folder.mkdir(parents=True)
        (folder / 'paper.sqlite3').touch()
        report = {'report_version': 1, 'instrument': 'BTC-USDT',
                  'status': 'qualified_segments_available' if qualified else 'more_data_needed',
                  'qualified_segments': ['1'] if qualified else [],
                  'segments': [{'failed_checks': [] if qualified else ['duration']}],
                  'simulated_fill_count': 0, 'accounting_verified': True}
        (folder / 'capture-review.json').write_text(json.dumps(report))
        return folder

    def test_empty_summary_does_not_create_directories_or_databases(self):
        result = snapshot(self.root, self.registry)
        self.assertEqual(result['saved_capture_count'], 0)
        self.assertEqual(result['next_action']['code'], 'collect_data')
        self.assertFalse(self.root.exists())

    def test_short_capture_shows_failed_checks_and_concrete_collection_command(self):
        self.save_capture()
        result = snapshot(self.root, self.registry)
        self.assertEqual(result['latest_capture']['failed_checks'], ['duration'])
        self.assertIn('--duration-seconds 1800', result['next_action']['command'])
        self.assertFalse(self.registry.exists())

    def test_qualified_capture_command_uses_discovered_path_and_shell_quoting(self):
        folder = self.save_capture(True)
        result = snapshot(self.root, self.registry)
        self.assertEqual(result['next_action']['code'], 'review_capture_workflow')
        self.assertEqual(shlex.split(result['next_action']['command'])[-1], str((folder / 'paper.sqlite3').resolve()))

    def test_workflow_failure_and_uncertain_running_state(self):
        folder = self.root / 'workflow-one'
        folder.mkdir(parents=True)
        path = folder / 'pipeline.json'
        state = {'workflow_version': 1, 'status': 'failed', 'phase': 'freezing_reference', 'error': 'bad input', 'journal': 'journal.sqlite3'}
        path.write_text(json.dumps(state))
        result = snapshot(self.root, self.registry)
        self.assertEqual(result['next_action']['code'], 'inspect_workflow')
        self.assertEqual(result['latest_workflow']['error'], 'bad input')
        state['status'] = 'running'
        path.write_text(json.dumps(state))
        self.assertIn('liveness is unknown', snapshot(self.root, self.registry)['next_action']['label'])

    def test_current_health_takes_priority_over_saved_success(self):
        self.save_capture(True)
        active = {'mode': 'paper', 'feed': {'status': 'disconnected'}, 'storage_error': None}
        self.assertEqual(snapshot(self.root, self.registry, active)['next_action']['code'], 'check_feed')
        active['feed']['status'] = 'healthy'
        self.assertEqual(snapshot(self.root, self.registry, active)['next_action']['code'], 'observe_capture')
        active['storage_error'] = 'disk full'
        self.assertEqual(snapshot(self.root, self.registry, active)['next_action']['code'], 'inspect_storage')

    def test_malformed_reports_are_reported_and_workflow_review_copies_ignored(self):
        folder = self.root / 'workflow-one'
        folder.mkdir(parents=True)
        (folder / 'pipeline.json').write_text('not-json')
        (folder / 'capture-review.json').write_text('{}')
        result = snapshot(self.root, self.registry)
        self.assertEqual(result['saved_capture_count'], 0)
        self.assertTrue(result['warnings'])

    def test_read_only_operations_endpoint(self):
        from app import server
        class Socket:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, *args):
                return io.BytesIO(b'GET /api/operations HTTP/1.0\r\nHost: localhost\r\n\r\n')
            def sendall(self, data):
                self.output.extend(data)
        connection = Socket()
        with patch.object(server, 'data_dir', self.root), patch.object(server, 'registry_path', self.registry), patch.object(server, 'engine', None), patch.object(server.Handler, 'log_message'):
            server.Handler(connection, ('127.0.0.1', 1), SimpleNamespace())
        headers, body = bytes(connection.output).split(b'\r\n\r\n', 1)
        self.assertIn(b'200 OK', headers)
        self.assertEqual(json.loads(body)['next_action']['code'], 'collect_data')
        self.assertFalse(self.root.exists())
