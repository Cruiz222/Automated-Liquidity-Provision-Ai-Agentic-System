import unittest
from decimal import Decimal

from app.amm_economics import AMMModelError, evaluate_constant_product_position, impermanent_loss_fraction


class TestAMMEconomics(unittest.TestCase):
    def test_no_price_change_has_zero_impermanent_loss(self):
        self.assertEqual(impermanent_loss_fraction("1"), Decimal("0"))

    def test_double_price_has_known_negative_il(self):
        result = impermanent_loss_fraction("2")
        self.assertLess(result, Decimal("0"))
        self.assertAlmostEqual(float(result), -0.0571909584, places=8)

    def test_unchanged_price_fees_increase_value_and_gas_reduces_it(self):
        result = evaluate_constant_product_position("1", "100", "100", "5", "2")
        self.assertEqual(result.initial_value_quote, Decimal("200"))
        self.assertEqual(result.hold_value_quote, Decimal("200"))
        self.assertEqual(result.lp_value_before_fees_quote, Decimal("200"))
        self.assertEqual(result.net_lp_value_quote, Decimal("203"))
        self.assertEqual(result.net_pnl_vs_hold_quote, Decimal("3"))

    def test_price_move_shows_il_against_hold(self):
        result = evaluate_constant_product_position("1", "100", "200")
        self.assertLess(result.lp_value_before_fees_quote, result.hold_value_quote)
        self.assertLess(result.net_pnl_vs_hold_quote, Decimal("0"))

    def test_fee_and_gas_inputs_must_be_nonnegative(self):
        with self.assertRaises(AMMModelError):
            evaluate_constant_product_position("1", "100", "100", "-1", "0")

    def test_reserves_and_price_must_be_positive(self):
        with self.assertRaises(AMMModelError):
            evaluate_constant_product_position("0", "100", "100")
        with self.assertRaises(AMMModelError):
            evaluate_constant_product_position("1", "100", "0")


if __name__ == "__main__":
    unittest.main()
