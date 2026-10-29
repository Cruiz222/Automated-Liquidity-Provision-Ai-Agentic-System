import copy
import json
from pathlib import Path
import tempfile
import unittest

from app.backtest import Execution, run
from app.engine import Config
from app.evaluate import digest, freeze
from app.fill_cost import attribute, study
from app.market_data import MarketEvent


def event(t, mid=100, side=None, price=None, size=None):
    return MarketEvent(t, mid - 2, mid + 2, 10, 10, price, size, side)


class FillCostTests(unittest.TestCase):
    def replay(self, events):
        return run(events, Config(half_spread_bps=100, inventory_skew_bps=0), Execution(0, 1))

    def test_partial_buys_and_sells_reconcile_by_hand(self):
        events = [event(0), event(100, side='sell', price=99, size=.4),
                  event(200, side='sell', price=99, size=.6),
                  event(300, side='buy', price=101, size=.25), event(400, mid=90)]
        replay = self.replay(events)
        result = attribute(events, replay, [100], 0)
        a = result['attribution']
        self.assertAlmostEqual(a['spread_capture'], 1.25)
        self.assertAlmostEqual(a['terminal_mid_movement'], -7.5)
        self.assertAlmostEqual(a['fees'], (99 + .25 * 101) * .0002)
        self.assertAlmostEqual(a['reconstructed_net_pnl'], 1.25 - 7.5 - a['fees'])
        self.assertAlmostEqual(a['accounting_residual'], 0)
        buy, _, sell = result['fills']
        self.assertAlmostEqual(buy['markouts'][0]['gross_markout'], .4)
        self.assertAlmostEqual(sell['markouts'][0]['gross_markout'], 2.75)
        self.assertAlmostEqual(sell['markouts'][0]['price_movement_pnl'], 2.5)
        summary = result['markout_summaries'][0]
        self.assertEqual(summary['observed_fill_count'], 3)
        self.assertAlmostEqual(summary['net_markout'], 3.75 - a['fees'])

    def test_first_target_row_ties_delays_and_segment_end(self):
        events = [event(0), event(100, side='sell', price=99, size=1),
                  event(200, mid=101), event(200, mid=103), event(1000, mid=90)]
        report = attribute(events, self.replay(events), [100, 200, 1000], 100)
        marks = report['fills'][0]['markouts']
        self.assertEqual(marks[0]['observation_ms'], 200)
        self.assertAlmostEqual(marks[0]['gross_markout'], 2)
        self.assertEqual(marks[1]['status'], 'observation_too_late')
        self.assertEqual(marks[1]['delay_ms'], 700)
        self.assertIsNone(marks[1]['net_markout'])
        self.assertEqual(marks[2]['status'], 'end_of_segment')
        self.assertIsNone(report['markout_summaries'][2]['net_markout_bps'])
        self.assertEqual(report['markout_summaries'][1]['observed_fill_count'], 0)
        accepted = attribute(events, self.replay(events), [200], 700)
        self.assertEqual(accepted['fills'][0]['markouts'][0]['status'], 'observed')

    def test_zero_fills_and_invalid_options(self):
        events = [event(0), event(100)]
        result = attribute(events, self.replay(events), [100])
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['attribution']['reconstructed_net_pnl'], 0)
        self.assertEqual(result['markout_summaries'][0]['observed_fill_count'], 0)
        for horizons, delay in (([], 0), ([0], 0), ([True], 0), ([1.5], 0), ([1, 1], 0),
                                (list(range(1, 18)), 0), ([1], -1), ([1], True)):
            with self.subTest(horizons=horizons, delay=delay), self.assertRaises(ValueError):
                attribute(events, self.replay(events), horizons, delay)

    def test_missing_fills_and_incorrect_accounting_fail(self):
        events = [event(0), event(100, side='sell', price=99, size=1)]
        replay = self.replay(events)
        changed = copy.deepcopy(replay)
        changed['fills'] = []
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            attribute(events, changed)
        changed = copy.deepcopy(replay)
        changed['metrics']['net_pnl'] += 1
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            attribute(events, changed)

    def test_manifest_integrity_and_development_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = (Path(__file__).resolve().parents[1] / 'examples/market_sample.csv').read_text()
            (root / 'dev.csv').write_text(raw)
            lines = raw.splitlines()
            shifted = [lines[0]] + [str(int(line.split(',')[0]) + 10000) + ',' + line.split(',', 1)[1] for line in lines[1:]]
            (root / 'test.csv').write_text('\n'.join(shifted) + '\n')
            plan = {'version': 1, 'instrument': 'TEST', 'datasets': [
                {'name': 'dev', 'path': 'dev.csv', 'role': 'development'},
                {'name': 'holdout', 'path': 'test.csv', 'role': 'test'}],
                'scenarios': [{'name': 'reference'}]}
            path = root / 'plan.json'
            path.write_text(json.dumps(plan))
            manifest = freeze(path)
            report = study(manifest, [100])
            self.assertEqual(len(report['results']), 1)
            self.assertEqual(report['results'][0]['dataset'], 'dev')
            self.assertEqual(report, study(manifest, [100]))
            no_dev = copy.deepcopy(manifest)
            no_dev['datasets'][0]['role'] = 'validation'
            no_dev['manifest_sha256'] = digest({k: v for k, v in no_dev.items() if k != 'manifest_sha256'})
            with self.assertRaisesRegex(ValueError, 'development datasets'):
                study(no_dev)
            (root / 'test.csv').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'Dataset changed'):
                study(manifest)
