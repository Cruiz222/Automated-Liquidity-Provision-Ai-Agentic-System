import unittest
from decimal import Decimal

from app.dex.backtest import (
    AccountingModelError,
    ExecutionCostConfig,
    HistoricalMarketData,
    LPAccountingModel,
    PricePoint,
    V2LPPortfolioModel,
)


class TestLPAccountingModel(unittest.TestCase):

    def setUp(self):
        self.portfolio_model = V2LPPortfolioModel()
        self.accounting_model = LPAccountingModel()

        market_data = HistoricalMarketData(
            pool_id="pool-1",
            token0="ETH",
            token1="USDT",
            observations=(
                PricePoint(0, 100),
                PricePoint(31536000, 100),
            ),
        )

        self.portfolio = self.portfolio_model.run(
            market_data,
            200,
        )

    def test_zero_apr_and_zero_costs_preserve_gross_value(self):
        result = self.accounting_model.run(
            self.portfolio,
            fee_apr=0,
        )

        for gross, net in zip(
            self.portfolio.observations,
            result.observations,
        ):
            self.assertEqual(
                net.net_lp_value_quote,
                gross.lp_value_quote,
            )

    def test_fee_accrual_uses_average_position_value(self):
        result = self.accounting_model.run(
            self.portfolio,
            fee_apr="0.10",
        )

        # $200 average position value × 10% annual APR.
        self.assertEqual(
            result.observations[-1].interval_fee_quote,
            Decimal("20"),
        )

        self.assertEqual(
            result.observations[-1].cumulative_fee_quote,
            Decimal("20"),
        )

    def test_entry_and_exit_gas_are_charged_once(self):
        costs = ExecutionCostConfig(
            entry_gas_quote=5,
            exit_gas_quote=7,
        )

        result = self.accounting_model.run(
            self.portfolio,
            fee_apr=0,
            costs=costs,
        )

        self.assertEqual(
            result.observations[0].entry_cost_quote,
            Decimal("5"),
        )

        self.assertEqual(
            result.observations[-1].exit_cost_quote,
            Decimal("7"),
        )

        self.assertEqual(
            result.observations[-1].cumulative_cost_quote,
            Decimal("12"),
        )

    def test_swap_costs_use_entry_and_exit_notionals(self):
        costs = ExecutionCostConfig(
            entry_swap_bps=100,
            exit_swap_bps=100,
        )

        result = self.accounting_model.run(
            self.portfolio,
            fee_apr=0,
            costs=costs,
        )

        # 1% of $200 entry capital and 1% of $200 exit value.
        self.assertEqual(
            result.observations[0].entry_cost_quote,
            Decimal("2"),
        )

        self.assertEqual(
            result.observations[-1].exit_cost_quote,
            Decimal("2"),
        )

    def test_net_value_includes_fees_and_deducts_costs(self):
        costs = ExecutionCostConfig(
            entry_gas_quote=5,
            exit_gas_quote=7,
        )

        result = self.accounting_model.run(
            self.portfolio,
            fee_apr="0.10",
            costs=costs,
        )

        final = result.observations[-1]

        self.assertEqual(
            final.net_lp_value_quote,
            Decimal("208"),
        )

        self.assertEqual(
            final.net_lp_pnl_quote,
            Decimal("8"),
        )

    def test_net_performance_is_compared_against_hold(self):
        result = self.accounting_model.run(
            self.portfolio,
            fee_apr=0,
            costs=ExecutionCostConfig(entry_gas_quote=5),
        )

        final = result.observations[-1]

        self.assertEqual(
            final.net_pnl_vs_hold_quote,
            Decimal("-5"),
        )

    def test_rejects_invalid_fee_apr(self):
        for value in (-0.1, "invalid", float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(AccountingModelError):
                    self.accounting_model.run(
                        self.portfolio,
                        fee_apr=value,
                    )

    def test_rejects_invalid_costs(self):
        with self.assertRaises(AccountingModelError):
            ExecutionCostConfig(entry_gas_quote=-1)

        with self.assertRaises(AccountingModelError):
            ExecutionCostConfig(entry_swap_bps=10000)

    def test_rejects_wrong_portfolio_type(self):
        with self.assertRaises(AccountingModelError):
            self.accounting_model.run(None, fee_apr=0)


if __name__ == "__main__":
    unittest.main()
