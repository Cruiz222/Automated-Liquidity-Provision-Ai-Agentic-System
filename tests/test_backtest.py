from dataclasses import replace
import unittest
from app.backtest import Execution, ReplaySession, compare, run
from app.engine import Config
from app.market_data import MarketEvent


def event(t, side=None, size=None, price=None, bid=98, ask=102):
    return MarketEvent(t, bid, ask, 10, 10, price, size, side)


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(half_spread_bps=100, inventory_skew_bps=0)
        self.execution = Execution(latency_ms=0, participation=1)

    def test_partial_fills_and_fee_accounting_by_hand(self):
        events = [event(0), event(100, 'sell', .4, 99),
                  event(200, 'sell', .6, 99), event(300, 'buy', .25, 101)]
        result = run(events, self.config, self.execution)
        m = result['metrics']
        self.assertEqual([f['size'] for f in result['fills']], [.4, .6, .25])
        self.assertAlmostEqual(m['final_inventory'], .75)
        expected_fee = (99 + 101 * .25) * .0002
        expected_cash = 100_000 - 99 + 101 * .25 - expected_fee
        self.assertAlmostEqual(m['fees'], expected_fee)
        self.assertAlmostEqual(m['final_cash'], expected_cash)
        self.assertAlmostEqual(m['net_pnl'], expected_cash + .75 * 100 - 100_000)
        self.assertAlmostEqual(m['liquidation_value_at_final_bid_after_fee'], expected_cash + .75 * 98 * .9998)
        self.assertAlmostEqual(m['time_weighted_inventory'], (0 + .4 + 1) / 3)
        self.assertEqual(m['submitted_quantity'], 4)
        self.assertAlmostEqual(m['quantity_fill_rate'], 1.25 / 4)

    def test_latency_and_same_row_lookahead(self):
        events = [event(0, 'sell', 10, 99), event(50, 'sell', 10, 99),
                  event(100, 'sell', 10, 99)]
        result = run(events, self.config, Execution(100, 1))
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['fills'][0]['event'], 2)
        self.assertEqual(result['fills'][0]['submitted_event'], 0)
        self.assertEqual(run(events[:2], self.config, Execution(100, 1))['fills'], [])

    def test_trade_participation_queue_and_remaining_quantity(self):
        events = [event(0), event(100, 'sell', 2, 99), event(150, 'sell', 2, 99),
                  event(160, 'sell', 20, 99)]
        result = run(events, self.config, Execution(100, .2, .5))
        self.assertEqual(len(result['fills']), 2)
        self.assertAlmostEqual(result['fills'][0]['size'], .3)
        self.assertAlmostEqual(result['fills'][1]['size'], .7)
        self.assertAlmostEqual(result['metrics']['final_inventory'], 1)

    def test_quotes_do_not_fill_without_compatible_trades(self):
        result = run([event(0), event(1), event(2, 'buy', 100, 99)], self.config, self.execution)
        self.assertEqual(result['fills'], [])

    def test_marketable_arrival_is_rejected(self):
        result = run([event(0), event(100, 'sell', 10, 97, bid=96, ask=98)], self.config, self.execution)
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['metrics']['post_only_rejections'], 1)

    def test_inventory_and_cash_limits(self):
        config = replace(self.config, initial_cash=150, max_inventory=1)
        events = [event(0)] + [event(i, 'sell', 100, 99) for i in range(1, 100)]
        result = run(events, config, self.execution)
        self.assertLessEqual(result['metrics']['max_inventory'], 1)
        self.assertGreaterEqual(result['metrics']['final_cash'], 0)
        self.assertEqual(result['metrics']['filled_quantity'], 1)

    def test_drawdown_includes_marked_inventory_loss(self):
        result = run([event(0), event(1, 'sell', 1, 99), event(2, bid=89, ask=91)], self.config, self.execution)
        self.assertAlmostEqual(result['metrics']['max_drawdown'], 10)
        self.assertGreater(result['metrics']['max_drawdown_pct'], 0)

    def test_prefix_is_independent_of_future_prices(self):
        prefix = [event(0), event(100, 'sell', 1, 99)]
        first = run(prefix + [event(200, bid=80, ask=82)], self.config, self.execution)
        second = run(prefix + [event(200, bid=110, ask=112)], self.config, self.execution)
        self.assertEqual(first['fills'], second['fills'])
        self.assertEqual(first['equity_curve'][:2], second['equity_curve'][:2])

    def test_baseline_identical_when_skew_is_zero_and_replay_repeatable(self):
        events = [event(0), event(100, 'sell', 1, 99), event(200, 'buy', 1, 101)]
        report = compare(events, self.config, self.execution)
        self.assertEqual(report['agent'], report['baseline'])
        self.assertEqual(report, compare(events, self.config, self.execution))

    def test_invalid_execution_and_zero_duration(self):
        for kwargs in ({'latency_ms': -1}, {'participation': 0}, {'participation': 2},
                       {'queue_ahead': float('nan')}, {'queue_ahead': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Execution(**kwargs)
        result = run([event(0), event(0)], self.config, self.execution)
        self.assertIsNone(result['metrics']['time_weighted_inventory'])
        self.assertEqual(result['metrics']['quantity_fill_rate'], 0)


class IncrementalSessionTests(unittest.TestCase):
    def test_bounded_history_keeps_cumulative_metrics(self):
        session = ReplaySession(Config(half_spread_bps=100, inventory_skew_bps=0),
                                Execution(0, 1), history_limit=2)
        session.step(event(0))
        for i in range(1, 7):
            session.step(event(i, 'sell' if i % 2 else 'buy', 1, 99 if i % 2 else 101))
        self.assertEqual(len(session.fills), 2)
        self.assertEqual(len(session.curve), 2)
        self.assertEqual(session.report()['metrics']['fill_count'], 6)
        self.assertEqual(session.report()['metrics']['filled_quantity'], 6)
