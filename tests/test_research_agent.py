from dataclasses import asdict
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app.backtest import Execution
from app.capture_review import Criteria, review
from app.engine import Config
from app.evaluate import evaluate, freeze, write_new
from app.paper import PaperRunner
from app.paper_store import PaperStore, export_csv
from app.research_agent import materialize, propose, request_openai, validate_decision


def payload(t, trades):
    return {'book': {'code': '0', 'data': [{'ts': str(t), 'bids': [['98', '10']], 'asks': [['102', '10']]}]},
            'trades': {'code': '0', 'data': [{'instId': 'BTC-USDT', 'tradeId': str(i), 'ts': str(ts), 'px': '100', 'sz': '1', 'side': 'sell'} for i, ts in trades]}}


class ResearchAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config, execution = Config(), Execution()
        store = PaperStore(self.root / 'run', {'version': 1, 'instrument': 'BTC-USDT', 'config': asdict(config), 'execution': asdict(execution)})
        now = [1000]
        runner = PaperRunner(store, config=config, execution=execution, clock=lambda: now[0])
        runner.ingest(payload(1000, [(1, 999)]))
        now[0] = 2000
        runner.ingest(payload(2000, [(1, 999), (2, 1900)]))
        runner.close()
        journal = self.root / 'run' / 'paper.sqlite3'
        export_csv(journal, self.root / 'export')
        plan = {'version': 1, 'instrument': 'BTC-USDT', 'datasets': [
            {'name': 'development', 'path': 'export/market-0001.csv', 'role': 'development'}],
            'scenarios': [{'name': 'original'}]}
        self.plan = self.root / 'plan.json'
        write_new(self.plan, plan)
        self.manifest = freeze(self.plan)
        self.manifest_path = self.root / 'manifest.json'
        write_new(self.manifest_path, self.manifest)
        self.capture_path = self.root / 'capture.json'
        self.capture = review(journal, Criteria(min_duration_ms=0, min_trades=1, min_healthy_poll_fraction=.5))
        write_new(self.capture_path, self.capture)
        self.journal = journal

    def test_offline_proposal_and_development_only_materialization(self):
        proposal = propose(self.capture_path, self.manifest_path)
        self.assertEqual(proposal['generator']['provider'], 'offline')
        self.assertEqual(proposal, propose(self.capture_path, self.manifest_path))
        manifest = materialize(proposal, self.manifest)
        self.assertEqual(len(manifest['scenarios']), 4)
        self.assertTrue(all(d['role'] == 'development' for d in manifest['datasets']))
        for scenario in manifest['scenarios']:
            self.assertEqual(scenario['config']['initial_cash'], self.manifest['scenarios'][0]['config']['initial_cash'])
            self.assertEqual(scenario['config']['max_inventory'], self.manifest['scenarios'][0]['config']['max_inventory'])
            self.assertEqual(scenario['config']['order_size'], self.manifest['scenarios'][0]['config']['order_size'])
        self.assertEqual(len(evaluate(manifest)['results']), 4)

    def test_short_capture_cannot_materialize_study(self):
        self.capture_path.write_text(json.dumps(review(self.journal)))
        proposal = propose(self.capture_path, self.manifest_path)
        self.assertEqual(proposal['decision']['action'], 'collect_more_data')
        with self.assertRaisesRegex(ValueError, 'requests more data'):
            materialize(proposal, self.manifest)

    def test_modified_proposals_evidence_and_unrelated_data_fail(self):
        proposal = propose(self.capture_path, self.manifest_path)
        modified = copy.deepcopy(proposal)
        modified['decision']['scenario_ids'] = ['arbitrary-code']
        with self.assertRaisesRegex(ValueError, 'Proposal was modified'):
            materialize(modified, self.manifest)
        self.capture_path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Capture evidence changed'):
            materialize(proposal, self.manifest)
        self.capture_path.write_text(json.dumps(self.capture))
        csv = self.root / 'export' / 'market-0001.csv'
        csv.write_text(csv.read_text().replace('98', '97'))
        different = freeze(self.plan)
        self.manifest_path.write_text(json.dumps(different))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            propose(self.capture_path, self.manifest_path)

    def test_model_sees_development_results_without_holdout_content(self):
        evaluation = {'manifest_sha256': self.manifest['manifest_sha256'], 'results': [
            {'role': 'development', 'scenario': 'original', 'event_count': 2, 'duration_ms': 900, 'agent': {}, 'baseline': {}},
            {'role': 'validation', 'private': 'SECRET_HOLDOUT_RESULT'}]}
        path = self.root / 'evaluation.json'
        write_new(path, evaluation)
        def opener(request, timeout):
            body = json.loads(request.data)
            self.assertNotIn('SECRET_HOLDOUT_RESULT', request.data.decode())
            self.assertFalse(body['store'])
            self.assertEqual(body['text']['format']['type'], 'json_schema')
            self.assertEqual(body['tools'] if 'tools' in body else [], [])
            decision = {'action': 'propose_study', 'rationale': 'Compare fixed stresses.', 'scenario_ids': ['latency-stress']}
            return io.StringIO(json.dumps({'id': 'test', 'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(decision)}]}]}))
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-only-secret'}):
            proposal = propose(self.capture_path, self.manifest_path, path, 'openai', 'test-model', opener)
        self.assertEqual(proposal['decision']['scenario_ids'], ['latency-stress'])

    def test_decision_boundary_rejects_actions_numbers_and_gate_bypass(self):
        for decision in (
            {'action': 'trade', 'rationale': 'x', 'scenario_ids': []},
            {'action': 'propose_study', 'rationale': 'x', 'scenario_ids': ['run-code']},
            {'action': 'collect_more_data', 'rationale': 'x', 'scenario_ids': [], 'cash': 1000000},
            {'action': 'propose_study', 'rationale': 'x', 'scenario_ids': ['latency-stress']}):
            with self.assertRaises(ValueError):
                validate_decision(decision, qualified=False)


class ResearchAPITests(unittest.TestCase):
    def test_incomplete_refusal_and_missing_credentials_fail(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
            request_openai({}, 'test-model')
        for result in ({'status': 'incomplete'}, {'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'refusal'}]}]}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                request_openai({}, 'test-model', key='secret', opener=lambda *a, **k: io.StringIO(json.dumps(result)))

    def test_http_errors_do_not_echo_credentials_or_error_bodies(self):
        def opener(*args, **kwargs):
            raise HTTPError('https://api.openai.com/v1/responses', 401, 'secret-key', {}, None)
        with self.assertRaises(ValueError) as context:
            request_openai({}, 'test-model', key='secret-key', opener=opener)
        self.assertNotIn('secret-key', str(context.exception))
        self.assertIn('401', str(context.exception))
