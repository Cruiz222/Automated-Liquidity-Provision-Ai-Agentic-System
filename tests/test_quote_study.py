import copy
import json
from pathlib import Path
import tempfile
import unittest

from app.evaluate import digest, freeze, verify_inputs
from app.quote_study import derive, run
from app.experiments import read_runs


class QuoteStudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        raw = (Path(__file__).resolve().parents[1] / 'examples/market_sample.csv').read_text()
        (self.root / 'dev.csv').write_text(raw)
        lines = raw.splitlines()
        shifted = [lines[0]] + [str(int(line.split(',')[0]) + 10000) + ',' + line.split(',', 1)[1] for line in lines[1:]]
        (self.root / 'holdout.csv').write_text('\n'.join(shifted) + '\n')
        plan = {'version': 1, 'instrument': 'SYNTHETIC-USD', 'datasets': [
            {'name': 'dev', 'path': 'dev.csv', 'role': 'development'},
            {'name': 'holdout', 'path': 'holdout.csv', 'role': 'test'}],
            'scenarios': [{'name': 'reference'}, {'name': 'stress', 'fee_bps': 3}]}
        path = self.root / 'plan.json'
        path.write_text(json.dumps(plan))
        self.base = freeze(path)
        self.path = self.root / 'base.lock.json'
        self.path.write_text(json.dumps(self.base))

    def test_only_half_spread_changes_and_holdouts_excluded(self):
        original = copy.deepcopy(self.base)
        result = derive(self.base, [0, 0.1, 12])
        self.assertEqual(self.base, original)
        self.assertEqual([d['name'] for d in result['datasets']], ['dev'])
        self.assertEqual(result['scenarios'][0], self.base['scenarios'][0])
        self.assertEqual(len(result['scenarios']), 3)
        for scenario in result['scenarios']:
            config = dict(scenario['config'])
            config['half_spread_bps'] = 12
            self.assertEqual(config, self.base['scenarios'][0]['config'])
            self.assertEqual(scenario['execution'], self.base['scenarios'][0]['execution'])
        verify_inputs(result)
        self.assertEqual(result['quote_study_provenance']['base_manifest_sha256'], self.base['manifest_sha256'])

    def test_invalid_inputs_create_no_workspace_or_registry(self):
        folder = self.root / 'out'
        registry = self.root / 'registry.sqlite3'
        for widths in ([], [-1], [float('nan')], [float('inf')], [True], [1, 1.0], list(range(17))):
            with self.subTest(widths=widths), self.assertRaises(ValueError):
                run(self.path, widths, workspace=folder, registry=registry)
        self.assertFalse(folder.exists())
        self.assertFalse(registry.exists())
        with self.assertRaisesRegex(ValueError, 'reference scenario'):
            derive(self.base, reference='missing')
        no_dev = copy.deepcopy(self.base)
        no_dev['datasets'][0]['role'] = 'validation'
        no_dev['manifest_sha256'] = digest({k: v for k, v in no_dev.items() if k != 'manifest_sha256'})
        with self.assertRaisesRegex(ValueError, 'development datasets'):
            derive(no_dev)

    def test_tampered_manifest_and_sources_are_rejected(self):
        changed = copy.deepcopy(self.base)
        changed['scenarios'][0]['config']['max_inventory'] = 100
        with self.assertRaisesRegex(ValueError, 'manifest was changed'):
            derive(changed)
        (self.root / 'holdout.csv').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Dataset changed'):
            derive(self.base)

    def test_registered_artifacts_reproduce_reference_and_protect_workspace(self):
        folder = self.root / 'out'
        registry = self.root / 'registry.sqlite3'
        result = run(self.path, [0.01, 12], workspace=folder, registry=registry)
        saved = read_runs(registry, result['experiment_id'])
        self.assertEqual(saved['status'], 'completed')
        report = json.loads(Path(saved['result_path']).read_text())
        self.assertEqual(report['roles_present'], ['development'])
        from app.evaluate import evaluate
        reference = evaluate(self.base)['results'][0]
        self.assertEqual(report['results'][0]['agent'], reference['agent'])
        self.assertEqual(json.loads((folder / 'summary.json').read_text()), result)
        # Failed overwrite keeps all artifacts intact and creates no extra experiment.
        with self.assertRaises(FileExistsError):
            run(self.path, workspace=folder, registry=registry)
        self.assertEqual(len(read_runs(registry)), 1)
        self.assertEqual(json.loads((folder / 'summary.json').read_text()), result)
