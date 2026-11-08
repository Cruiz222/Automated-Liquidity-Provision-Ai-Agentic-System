import unittest
from decimal import Decimal

from app.dex.backtest import (
    HistoricalMarketData,
    PortfolioModelError,
    PricePoint,
    V2LPPortfolioModel,
)


class TestV2LPPortfolioModel(unittest.TestCase):

    def setUp(self):
        self.model = V2LPPortfolioModel()

    def make_market_data(self, prices):
        return HistoricalMarketData(
            pool_id="pool-1",
            chain="ethereum",
            token0="ETH",
            token1="USDT",
            observations=tuple(
                PricePoint(timestamp=timestamp, price=price)
                for timestamp, price in prices
            ),
        )

    def test_initial_position_is_balanced_by_value(self):
        data = self.make_market_data([(100, 100)])
        result = self.model.run(data, 200)

        self.assertEqual(
            result.initial_amount_base,
            Decimal("1"),
        )
        self.assertEqual(
            result.initial_amount_quote,
            Decimal("100"),
        )

        point = result.observations[0]

        self.assertEqual(point.lp_value_quote, Decimal("200"))
        self.assertEqual(point.hold_value_quote, Decimal("200"))
        self.assertEqual(point.impermanent_loss_fraction, Decimal("0"))

    def test_no_price_movement_produces_no_pnl_or_il(self):
        data = self.make_market_data([
            (100, 100),
            (200, 100),
            (300, 100),
        ])

        result = self.model.run(data, 200)

        for point in result.observations:
            self.assertEqual(point.lp_value_quote, Decimal("200"))
            self.assertEqual(point.hold_value_quote, Decimal("200"))
            self.assertEqual(point.lp_pnl_quote, Decimal("0"))
            self.assertEqual(point.hold_pnl_quote, Decimal("0"))
            self.assertEqual(point.impermanent_loss_fraction, Decimal("0"))

    def test_price_doubling_changes_lp_token_composition(self):
        data = self.make_market_data([
            (100, 100),
            (200, 200),
        ])

        result = self.model.run(data, 200)
        point = result.observations[-1]

        self.assertLess(point.lp_amount_base, Decimal("1"))
        self.assertGreater(point.lp_amount_quote, Decimal("100"))

        self.assertAlmostEqual(
            float(point.lp_amount_base),
            1 / (2 ** 0.5),
            places=12,
        )

        self.assertAlmostEqual(
            float(point.lp_amount_quote),
            100 * (2 ** 0.5),
            places=12,
        )

    def test_lp_value_and_hold_value_are_different_after_price_move(self):
        data = self.make_market_data([
            (100, 100),
            (200, 200),
        ])

        result = self.model.run(data, 200)
        point = result.observations[-1]

        self.assertAlmostEqual(
            float(point.lp_value_quote),
            200 * (2 ** 0.5),
            places=10,
        )
        self.assertEqual(point.hold_value_quote, Decimal("300"))
        self.assertLess(point.lp_value_quote, point.hold_value_quote)

    def test_il_matches_existing_amm_calculation(self):
        data = self.make_market_data([
            (100, 100),
            (200, 200),
        ])

        result = self.model.run(data, 200)
        point = result.observations[-1]

        self.assertAlmostEqual(
            float(point.impermanent_loss_fraction),
            -0.0571909584,
            places=9,
        )

    def test_pnl_is_reported_against_initial_capital_and_hold(self):
        data = self.make_market_data([
            (100, 100),
            (200, 200),
        ])

        result = self.model.run(data, 200)
        point = result.observations[-1]

        self.assertEqual(
            point.lp_pnl_quote.quantize(Decimal("0.000000000001")),
            (point.lp_value_quote - Decimal("200")).quantize(
            Decimal("0.000000000001")
            ),
        )
        self.assertEqual(
            point.hold_pnl_quote,
            Decimal("100"),
        )
        self.assertEqual(
            point.pnl_vs_hold_quote.quantize(Decimal("0.000000000001")),
            (point.lp_value_quote - Decimal("300")).quantize(
            Decimal("0.000000000001")
            ),
        )

    def test_observations_remain_chronological(self):
        data = self.make_market_data([
            (100, 100),
            (200, 120),
            (300, 90),
        ])

        result = self.model.run(data, 200)

        timestamps = [
            point.timestamp for point in result.observations
        ]

        self.assertEqual(timestamps, [100, 200, 300])

    def test_rejects_invalid_initial_capital(self):
        data = self.make_market_data([(100, 100)])

        for capital in (0, -100, "invalid", float("inf"), float("nan")):
            with self.subTest(capital=capital):
                with self.assertRaises(PortfolioModelError):
                    self.model.run(data, capital)

    def test_rejects_invalid_market_data_type(self):
        with self.assertRaises(PortfolioModelError):
            self.model.run(None, 200)


if __name__ == "__main__":
    unittest.main()
