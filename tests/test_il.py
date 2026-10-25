import math
import unittest

from app.dex import (
    annualized_volatility, expected_v2_il, v2_il, v3_capital_multiplier, v3_holdings, v3_il,
)


class TestImpermanentLoss(unittest.TestCase):
    def test_known_v2_values(self):
        self.assertAlmostEqual(v2_il(1), 0.0)
        self.assertAlmostEqual(v2_il(2), -0.0571909, places=6)
        self.assertAlmostEqual(v2_il(4), -0.2, places=9)
        self.assertAlmostEqual(v2_il(0.5), v2_il(2), places=12)   # symmetric in log price

    def test_v2_rejects_bad_ratio(self):
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                v2_il(bad)

    def test_wide_v3_range_matches_v2(self):
        self.assertAlmostEqual(v3_il(1.0, 2.0, 1e-9, 1e9), v2_il(2.0), places=4)

    def test_narrow_range_loses_more_than_v2(self):
        self.assertLess(v3_il(1.0, 1.2, 0.9, 1.1), v2_il(1.2))

    def test_v3_no_move_no_loss(self):
        self.assertAlmostEqual(v3_il(1.0, 1.0, 0.5, 2.0), 0.0)

    def test_holdings_in_each_region(self):
        x, y = v3_holdings(1.0, 0.4, 0.5, 2.0)
        self.assertEqual(y, 0.0)
        self.assertGreater(x, 0)
        x, y = v3_holdings(1.0, 3.0, 0.5, 2.0)
        self.assertEqual(x, 0.0)
        self.assertGreater(y, 0)

    def test_capital_multiplier(self):
        self.assertAlmostEqual(v3_capital_multiplier(1.0, 0.5, 2.0), 1 / (1 - 0.25 ** 0.25), places=6)
        with self.assertRaises(ValueError):
            v3_capital_multiplier(1.0, 2.0, 0.5)

    def test_expected_il_matches_small_variance_approximation(self):
        vol, days = 0.2, 30
        approx = vol ** 2 * days / 365 / 8
        self.assertAlmostEqual(expected_v2_il(vol, days) / approx, 1.0, delta=0.02)

    def test_expected_il_grows_with_volatility_and_time(self):
        self.assertLess(expected_v2_il(0.3, 30), expected_v2_il(0.6, 30))
        self.assertLess(expected_v2_il(0.3, 30), expected_v2_il(0.3, 90))
        self.assertEqual(expected_v2_il(0, 30), 0.0)

    def test_volatility_of_known_series(self):
        prices = [100 * math.exp(0.01 * (-1) ** i) for i in range(30)]
        self.assertGreater(annualized_volatility(prices), 0.3)
        self.assertAlmostEqual(annualized_volatility([1, 1, 1, 1]), 0.0)
        with self.assertRaises(ValueError):
            annualized_volatility([1, 2])


if __name__ == "__main__":
    unittest.main()
