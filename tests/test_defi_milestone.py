import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from app.wallet_ledger import (
    IdempotencyConflict, InsufficientFunds, InvalidOperation, WalletLedger
)
from app.defi_milestone import ConversionQuotes, LiquidityPositions, QuoteExpired


class TestConversionQuotes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = WalletLedger(str(Path(self.temp.name) / "wallet.sqlite3"))
        self.quotes = ConversionQuotes(self.ledger, Decimal("1500"), fee_bps=100, ttl_seconds=60)
        self.ledger.deposit("u1", "NGN", 150_000, "ngn-deposit")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_quote_ngn_to_usdt_applies_fee(self):
        quote = self.quotes.create_quote("u1", "NGN", 150_000)
        # 1,500 NGN at 1,500 NGN/USDT, less a 1% fee = 0.99 USDT.
        self.assertEqual(quote["target_currency"], "USDT")
        self.assertEqual(quote["target_amount_minor"], 990_000)

    def test_quote_usdt_to_ngn(self):
        self.ledger.deposit("u1", "USDT", 1_000_000, "usdt-deposit")
        quote = self.quotes.create_quote("u1", "USDT", 1_000_000)
        self.assertEqual(quote["target_currency"], "NGN")
        self.assertEqual(quote["target_amount_minor"], 148_500)

    def test_execute_quote_moves_balances(self):
        quote = self.quotes.create_quote("u1", "NGN", 150_000)
        self.quotes.execute(quote["quote_id"], "convert-1")
        self.assertEqual(self.ledger.balance("u1", "NGN"), 0)
        self.assertEqual(self.ledger.balance("u1", "USDT"), 990_000)

    def test_quote_cannot_be_consumed_twice(self):
        quote = self.quotes.create_quote("u1", "NGN", 150_000)
        self.quotes.execute(quote["quote_id"], "convert-1")
        with self.assertRaises(QuoteExpired):
            self.quotes.execute(quote["quote_id"], "convert-2")

    def test_same_execution_retry_is_idempotent(self):
        quote = self.quotes.create_quote("u1", "NGN", 150_000)
        first = self.quotes.execute(quote["quote_id"], "convert-1")
        second = self.quotes.execute(quote["quote_id"], "convert-1")
        self.assertEqual(first, second)
        self.assertEqual(self.ledger.balance("u1", "USDT"), 990_000)

    def test_insufficient_funds_does_not_consume_quote(self):
        quote = self.quotes.create_quote("u1", "NGN", 200_000)
        with self.assertRaises(InsufficientFunds):
            self.quotes.execute(quote["quote_id"], "convert-1")
        # The quote remains unconsumed, although this wallet still lacks funds.
        with self.assertRaises(InsufficientFunds):
            self.quotes.execute(quote["quote_id"], "convert-2")

    def test_invalid_rate_rejected(self):
        with self.assertRaises(InvalidOperation):
            ConversionQuotes(self.ledger, "0")


class TestLiquidityPositions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = WalletLedger(str(Path(self.temp.name) / "wallet.sqlite3"))
        self.positions = LiquidityPositions(self.ledger)
        self.ledger.deposit("u1", "USDT", 5_000_000, "usdt-deposit")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_open_position_reserves_available_usdt(self):
        result = self.positions.open_position(
            "u1", "pool-eth-usdt", "ETH", "USDT", 2_000_000, "open-1"
        )
        self.assertEqual(result["status"], "active")
        self.assertEqual(self.ledger.balance("u1", "USDT"), 3_000_000)
        self.assertEqual(self.positions.get_position(result["position_id"])["status"], "active")

    def test_cannot_open_position_without_funds(self):
        with self.assertRaises(InsufficientFunds):
            self.positions.open_position(
                "u1", "pool-x", "AAA", "BBB", 9_000_000, "open-1"
            )
        self.assertEqual(self.ledger.balance("u1", "USDT"), 5_000_000)

    def test_mark_to_market_reports_pnl_including_fees(self):
        result = self.positions.open_position(
            "u1", "pool-eth-usdt", "ETH", "USDT", 2_000_000, "open-1"
        )
        position = self.positions.mark_to_market(
            result["position_id"], 1_800_000, 100_000
        )
        self.assertEqual(position["unrealized_pnl_usdt_micro"], -100_000)

    def test_close_returns_simulated_value_plus_fees(self):
        result = self.positions.open_position(
            "u1", "pool-eth-usdt", "ETH", "USDT", 2_000_000, "open-1"
        )
        self.positions.mark_to_market(result["position_id"], 1_800_000, 100_000)
        closed = self.positions.close_position(result["position_id"], "close-1")
        self.assertEqual(closed["returned_usdt_micro"], 1_900_000)
        self.assertEqual(self.ledger.balance("u1", "USDT"), 4_900_000)
        self.assertEqual(self.positions.get_position(result["position_id"])["status"], "closed")

    def test_open_retry_does_not_reserve_twice(self):
        args = ("u1", "pool-eth-usdt", "ETH", "USDT", 2_000_000, "open-1")
        first = self.positions.open_position(*args)
        second = self.positions.open_position(*args)
        self.assertEqual(first, second)
        self.assertEqual(self.ledger.balance("u1", "USDT"), 3_000_000)

    def test_reusing_key_with_different_request_fails(self):
        self.positions.open_position(
            "u1", "pool-eth-usdt", "ETH", "USDT", 2_000_000, "open-1"
        )
        with self.assertRaises(IdempotencyConflict):
            self.positions.open_position(
                "u1", "pool-btc-usdt", "BTC", "USDT", 2_000_000, "open-1"
            )

    def test_closed_position_cannot_be_marked_again(self):
        result = self.positions.open_position(
            "u1", "pool-eth-usdt", "ETH", "USDT", 2_000_000, "open-1"
        )
        self.positions.close_position(result["position_id"], "close-1")
        with self.assertRaises(InvalidOperation):
            self.positions.mark_to_market(result["position_id"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
