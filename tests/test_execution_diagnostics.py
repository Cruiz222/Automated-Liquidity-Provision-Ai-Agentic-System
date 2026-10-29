import unittest
from app.backtest import Execution, ReplaySession
from app.engine import Config
from app.market_data import MarketEvent


def event(t, side=None, price=99, size=1):
    return MarketEvent(t, 98, 102, 10, 10, price if side else None, size if side else None, side)


class ExecutionDiagnosticsTests(unittest.TestCase):
    def session(self, latency=0, queue=0):
        return ReplaySession(Config(half_spread_bps=100, inventory_skew_bps=0), Execution(latency, 1, queue))

    def test_latency_price_and_filled_outcomes(self):
        s = self.session(latency=100)
        s.step(event(0))
        s.step(event(50, 'sell'))
        self.assertEqual(s.last_execution['outcome'], 'waiting_latency')
        s.step(event(100, 'sell', price=100))
        self.assertEqual(s.last_execution['outcome'], 'price_not_reached')
        self.assertGreater(s.last_execution['distance_bps'], 0)
        s.step(event(150, 'sell'))
        self.assertEqual(s.last_execution['outcome'], 'filled')
        self.assertEqual(sum(s.execution_counts.values()), 3)

    def test_queue_pause_and_no_inventory_quote(self):
        s = self.session(queue=2)
        s.step(event(0))
        s.step(event(1, 'sell'))
        self.assertEqual(s.last_execution['outcome'], 'queue_ahead')
        s.step(event(2, 'buy', price=101))
        self.assertEqual(s.last_execution['outcome'], 'no_active_quote')
        s.step(event(3, 'sell'), trade=False, submit=False)
        self.assertEqual(s.last_execution['outcome'], 'paused')
        self.assertEqual(s.account.inventory, 0)

    def test_marketable_rejection_is_identified(self):
        s = self.session()
        s.step(event(0))
        s.step(MarketEvent(1, 96, 98, 10, 10, 97, 1, 'sell'))
        self.assertEqual(s.last_execution['outcome'], 'post_only_rejected')
