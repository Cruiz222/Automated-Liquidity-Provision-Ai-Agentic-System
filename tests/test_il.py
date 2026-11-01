import math
import unittest

from app.dex import (
    annualized_volatility,
    annualized_volatility_from_observations,
    expected_v2_il,
    v2_il,
    v3_capital_multiplier,
    v3_holdings,
    v3_il,
)


class TestImpermanentLoss(unittest.TestCase):
    def test_known_v2_values(self):
        self.assertAlmostEqual(v2_il(1), 0.0)
        self.assertAlmostEqual(v2_il(2), -0.0571909, places=6)
        self.assertAlmostEqual(v2_il(4), -0.2, places=9)
        self.assertAlmostEqual(v2_il(0.5), v2_il(2), places=12)

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

    def test_timestamped_volatility_is_resolution_independent(self):
        hourly_returns = [0.002, -0.001, 0.003, -0.002] * 12
        daily_returns = [sum(hourly_returns[i:i + 24]) for i in range(0, 0, 24)]
        # Use the same constant variance rate with a deterministic alternating
        # path: daily increments are 24 hourly increments aggregated in blocks.
        daily_returns = [
            0.01 * math.sqrt(24) * (-1) ** i
            for i in range(4)
        ]
        hourly_prices = [100.0]
        for value in hourly_returns:
            hourly_prices.append(hourly_prices[-1] * math.exp(value))
        daily_prices = [100.0]
        for value in daily_returns:
            daily_prices.append(daily_prices[-1] * math.exp(value))
        hourly = [
            (i * 3600.0, price)
            for i, price in enumerate(hourly_prices)
        ]
        daily = [
            (i * 86400.0, price)
            for i, price in enumerate(daily_prices)
        ]
        self.assertAlmostEqual(
            annualized_volatility_from_observations(hourly),
            annualized_volatility_from_observations(daily),
            places=10,
        )

    def test_timestamped_volatility_uses_actual_irregular_elapsed_time(self):
        # Identical log-return variance rate despite different interval lengths.
        observations = [
            (0.0, 100.0),
            (3600.0, 100.0 * math.exp(0.01)),
            (10800.0, 100.0 * math.exp(0.01 + 0.01 * math.sqrt(2))),
            (14400.0, 100.0 * math.exp(0.02 + 0.01 * math.sqrt(2))),
        ]
        expected = math.sqrt(
            (0.01**2 + (0.01 * math.sqrt(2))**2 + 0.01**2)
            / 14400.0
            * (365.0 * 24.0 * 3600.0)
        )
        self.assertAlmostEqual(
            annualized_volatility_from_observations(observations),
            expected,
        )

    def test_timestamped_volatility_rejects_excessive_gaps(self):
        with self.assertRaisesRegex(ValueError, "exceeds maximum"):
            annualized_volatility_from_observations(
                [(0.0, 1.0), (3600.0, 1.01), (4 * 3600.0, 1.02)],
                max_gap_seconds=2 * 3600.0,
            )

    def test_timestamped_volatility_rejects_invalid_timestamps_and_prices(self):
        invalid = (
            [(0.0, 1.0), (0.0, 1.1), (3600.0, 1.2)],
            [(0.0, 1.0), (True, 1.1), (3600.0, 1.2)],
            [(0.0, 1.0), (3600.0, 0.0), (7200.0, 1.2)],
        )
        for observations in invalid:
            with self.subTest(observations=observations):
                with self.assertRaises(ValueError):
                    annualized_volatility_from_observations(observations)

    def test_timestamped_volatility_requires_three_observations(self):
        with self.assertRaisesRegex(ValueError, "at least 3"):
            annualized_volatility_from_observations([(0.0, 1.0), (3600.0, 1.1)])

    def test_timestamped_flat_series_has_zero_volatility(self):
        observations = [(i * 3600.0, 1.0) for i in range(5)]
        self.assertEqual(annualized_volatility_from_observations(observations), 0.0)


if __name__ == "__main__":
    unittest.main()
