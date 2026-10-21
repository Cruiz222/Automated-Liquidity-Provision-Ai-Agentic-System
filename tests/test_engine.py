import unittest
from app.engine import Config, Engine


class EngineTests(unittest.TestCase):
    def test_long_run_respects_limits_and_accounting(self):
        engine = Engine()
        for _ in range(10000):
            engine.step()
            state = engine.snapshot()
            self.assertGreaterEqual(state['cash'], -1e-8)
            self.assertGreaterEqual(state['inventory'], 0)
            self.assertLessEqual(state['inventory'], engine.config.max_inventory)
            self.assertAlmostEqual(state['equity'], state['cash'] + state['inventory'] * state['price'])
        self.assertGreater(state['fees'], 0)

    def test_pause_freezes_state(self):
        engine = Engine()
        engine.set_running(False)
        before = engine.snapshot()
        engine.step()
        self.assertEqual(before, engine.snapshot())

    def test_reproducible_simulation(self):
        a, b = Engine(seed=42), Engine(seed=42)
        for _ in range(100):
            a.step()
            b.step()
        self.assertEqual(a.snapshot(), b.snapshot())

    def test_inventory_skews_quotes_and_blocks_buys(self):
        engine = Engine()
        bid = engine.quotes()['bid']
        engine.inventory = 5
        self.assertLess(engine.quotes()['bid'], bid)
        engine.inventory = engine.config.max_inventory
        self.assertIsNone(engine.quotes()['bid'])
        engine.inventory = 0
        self.assertIsNone(engine.quotes()['ask'])

    def test_invalid_config(self):
        for kwargs in ({'order_size': 0}, {'fee_bps': -1}, {'initial_price': float('nan')}):
            with self.assertRaises(ValueError):
                Config(**kwargs)


if __name__ == '__main__':
    unittest.main()
