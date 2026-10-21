import copy
import json
from pathlib import Path
import tempfile
import unittest

from app.evaluate import digest, evaluate, freeze, write_new


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        raw = (Path(__file__).resolve().parents[1] / 'examples' / 'market_sample.csv').read_text()
        (self.root / 'first.csv').write_text(raw)
        lines = raw.splitlines()
        shifted = [lines[0]] + [str(int(row.split(',')[0]) + 10000) + ',' + row.split(',', 1)[1] for row in lines[1:]]
        (self.root / 'second.csv').write_text('\n'.join(shifted) + '\n')
        self.plan = {'version': 1, 'instrument': 'SYNTHETIC-USD', 'datasets': [
            {'name': 'first', 'path': 'first.csv', 'role': 'development'},
            {'name': 'second', 'path': 'second.csv', 'role': 'validation'}],
            'scenarios': [{'name': 'default'}, {'name': 'slow', 'execution': {'latency_ms': 10000}}]}
        self.path = self.root / 'plan.json'

    def lock(self):
        self.path.write_text(json.dumps(self.plan))
        return freeze(self.path)

    def test_separate_accounts_roles_and_repeatability(self):
        manifest = self.lock()
        report = evaluate(manifest)
        self.assertEqual(report, evaluate(manifest))
        self.assertEqual(len(report['results']), 4)
        first, second = report['results'][:2]
        self.assertEqual(first['agent']['metrics'], second['agent']['metrics'])
        self.assertEqual(report['roles_present'], ['development', 'validation'])
        self.assertTrue(all(row['agent']['metrics']['fill_count'] == 0 for row in report['results'][2:]))
        self.assertEqual(len(report['summaries']), 4)

    def test_source_and_manifest_changes_are_rejected(self):
        manifest = self.lock()
        modified = copy.deepcopy(manifest)
        modified['scenarios'][0]['execution']['latency_ms'] = 0
        with self.assertRaisesRegex(ValueError, 'manifest was changed'):
            evaluate(modified)
        (self.root / 'first.csv').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Dataset changed'):
            evaluate(manifest)

    def test_duplicate_content_and_cross_role_overlap_are_rejected(self):
        (self.root / 'second.csv').write_bytes((self.root / 'first.csv').read_bytes())
        with self.assertRaisesRegex(ValueError, 'Duplicate dataset'):
            self.lock()
        raw = (self.root / 'first.csv').read_text().replace('99.50', '99.49')
        (self.root / 'second.csv').write_text(raw)
        with self.assertRaisesRegex(ValueError, 'overlap across roles'):
            self.lock()
        self.plan['datasets'][1]['role'] = 'development'
        with self.assertRaisesRegex(ValueError, 'overlap'):
            self.lock()

    def test_invalid_scenarios_and_output_protection(self):
        self.plan['scenarios'][1]['name'] = 'default'
        with self.assertRaisesRegex(ValueError, 'Scenario names'):
            self.lock()
        self.plan['scenarios'][1]['name'] = 'slow'
        self.plan['scenarios'][1]['execution']['participation'] = 2
        with self.assertRaises(ValueError):
            self.lock()
        output = self.root / 'result.json'
        write_new(output, {'keep': True})
        with self.assertRaises(FileExistsError):
            write_new(output, {})
        self.assertEqual(json.loads(output.read_text()), {'keep': True})
