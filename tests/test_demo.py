import unittest

from app.demo import run


class TestDemo(unittest.TestCase):
    def test_end_to_end_scenario_reconciles(self):
        report = run()
        self.assertTrue(report["reconciliation"]["ok"], report["reconciliation"])
        self.assertEqual(report["withdrawal"], "completed")
        self.assertEqual(report["conversion"], "simulated")
        self.assertGreaterEqual(report["positions_opened"], 3)
        self.assertGreater(report["fees_booked"]["USDT"], 0)
        # NGN 5.0M at an ask of ~1,515 NGN/USDT less fees: total value must be in a sane range.
        self.assertTrue(2_900_000_000 < report["wallet"]["total_value_usdt_micro"] < 3_400_000_000)


if __name__ == "__main__":
    unittest.main()
