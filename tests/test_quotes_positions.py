import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.fx import ConversionQuotes, QuoteError, QuoteExpired, RateService, StaticRateSource
from app.ledger import IdempotencyConflict, InsufficientFunds, InvalidOperation, WalletLedger
from app.portfolio import LiquidityPositions, PositionNotFound

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
        self.quotes.execute(quote["quote_id"], "convert-1", account="u1")
        self.assertEqual(self.ledger.balance("u1", "NGN"), 0)
        self.assertEqual(self.ledger.balance("u1", "USDT"), 990_000)

    def test_quote_cannot_be_consumed_twice(self):
        quote = self.quotes.create_quote("u1", "NGN", 150_000)
        self.quotes.execute(quote["quote_id"], "convert-1", account="u1")
        with self.assertRaises(QuoteExpired):
            self.quotes.execute(quote["quote_id"], "convert-2", account="u1")

    def test_same_execution_retry_is_idempotent(self):
        quote = self.quotes.create_quote("u1", "NGN", 150_000)
        first = self.quotes.execute(quote["quote_id"], "convert-1", account="u1")
        second = self.quotes.execute(quote["quote_id"], "convert-1", account="u1")
        self.assertEqual(first, second)
        self.assertEqual(self.ledger.balance("u1", "USDT"), 990_000)

    def test_insufficient_funds_does_not_consume_quote(self):
        quote = self.quotes.create_quote("u1", "NGN", 200_000)
        with self.assertRaises(InsufficientFunds):
            self.quotes.execute(quote["quote_id"], "convert-1", account="u1")
        # The quote remains unconsumed, although this wallet still lacks funds.
        with self.assertRaises(InsufficientFunds):
            self.quotes.execute(quote["quote_id"], "convert-2", account="u1")

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
        closed = self.positions.close_position(result["position_id"], "close-1", account="u1")
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
        self.positions.close_position(result["position_id"], "close-1", account="u1")
        with self.assertRaises(InvalidOperation):
            self.positions.mark_to_market(result["position_id"], 1_000_000)


class TestQuoteCorrections(unittest.TestCase):
    def setUp(self):
        self.ledger = WalletLedger()
        self.addCleanup(self.ledger.close)
        self.clock = [1_000.0]
        self.rates = RateService([StaticRateSource("1500", 200, "feed", lambda: self.clock[0])],
                                 max_age_seconds=60, clock=lambda: self.clock[0])
        self.quotes = ConversionQuotes(self.ledger, fee_bps=100, ttl_seconds=60, rate_service=self.rates)
        self.ledger.deposit("u1", "NGN", 15_000_000, "d-ngn")
        self.ledger.deposit("u1", "USDT", 100_000_000, "d-usdt")

    def test_buying_usdt_uses_ask_and_selling_uses_bid(self):
        buy = self.quotes.create_quote("u1", "NGN", 15_000_000)
        sell = self.quotes.create_quote("u1", "USDT", 100_000_000)
        self.assertEqual(Decimal(buy["rate"]), Decimal("1515"))
        self.assertEqual(Decimal(sell["rate"]), Decimal("1485"))
        self.assertEqual(buy["rate_source"], "feed")
        self.assertEqual(buy["rate_as_of"], 1_000.0)

    def test_stale_rate_blocks_new_quotes(self):
        self.clock[0] += 500
        self.rates.sources[0]._clock = lambda: 1_000.0
        with self.assertRaises(QuoteError):
            self.quotes.create_quote("u1", "NGN", 15_000_000)

    def test_fee_is_booked_to_fees_account(self):
        quote = self.quotes.create_quote("u1", "NGN", 15_000_000, rate="1500", now=0)
        self.assertEqual(quote["target_amount_minor"], 99_000_000)
        self.quotes.execute(quote["quote_id"], "c1", account="u1", now=1)
        self.assertEqual(self.ledger.balance("fees", "USDT"), quote["fee_minor"])
        self.assertEqual(quote["fee_minor"], 1_000_000)
        self.assertEqual(quote["rate_source"], "override")
        self.assertTrue(self.ledger.reconcile()["ok"])

    def test_customer_cannot_use_system_account_names(self):
        with self.assertRaises(InvalidOperation):
            self.ledger.convert("fees", "u1", "USDT", "NGN", 1, 1, "x")


class TestPositionCorrections(unittest.TestCase):
    def setUp(self):
        self.ledger = WalletLedger()
        self.addCleanup(self.ledger.close)
        self.positions = LiquidityPositions(self.ledger)
        self.ledger.deposit("u1", "USDT", 100_000_000, "d1")
        self.pos = self.positions.open_position("u1", "pool-a", "WETH", "USDC", 50_000_000, "o1")["position_id"]

    def test_position_records_its_pair(self):
        position = self.positions.get_position(self.pos)
        self.assertEqual((position["asset"], position["quote_asset"]), ("WETH", "USDC"))

    def test_absurd_mark_is_rejected(self):
        # Regression: a 99 USDT "fee" on a 50 USDT position used to be paid out unchecked.
        with self.assertRaises(InvalidOperation):
            self.positions.mark_to_market(self.pos, 50_000_000, 99_000_000)
        self.assertEqual(self.positions.get_position(self.pos)["accrued_fees_minor"], 0)

    def test_fees_cannot_decrease_and_marks_cannot_go_back_in_time(self):
        self.positions.mark_to_market(self.pos, 50_000_000, 1_000_000, source="a", as_of=100)
        with self.assertRaises(InvalidOperation):
            self.positions.mark_to_market(self.pos, 50_000_000, 500_000, as_of=200)
        with self.assertRaises(InvalidOperation):
            self.positions.mark_to_market(self.pos, 50_000_000, 1_000_000, as_of=50)

    def test_marks_are_kept_with_their_source(self):
        self.positions.mark_to_market(self.pos, 49_000_000, 100_000, source="subgraph", as_of=100)
        self.positions.mark_to_market(self.pos, 48_000_000, 200_000, source="subgraph", as_of=200)
        marks = self.positions.marks(self.pos)
        self.assertEqual([m["source"] for m in marks], ["subgraph", "subgraph"])
        self.assertEqual([m["value_minor"] for m in marks], [49_000_000, 48_000_000])

    def test_close_records_realized_pnl_and_retry_is_safe(self):
        self.positions.mark_to_market(self.pos, 48_000_000, 1_000_000, as_of=100)
        first = self.positions.close_position(self.pos, "c1", account="u1")
        second = self.positions.close_position(self.pos, "c1", account="u1")
        self.assertEqual(first, second)
        self.assertEqual(first["realized_pnl_minor"], -1_000_000)
        self.assertEqual(self.ledger.balance("u1", "USDT"), 99_000_000)
        self.assertEqual(self.positions.get_position(self.pos)["realized_pnl_minor"], -1_000_000)
        self.assertTrue(self.ledger.reconcile()["ok"])

    def test_close_key_reused_for_another_position_is_a_conflict(self):
        other = self.positions.open_position("u1", "pool-b", "WBTC", "USDC", 10_000_000, "o2")["position_id"]
        self.positions.close_position(self.pos, "c1", account="u1")
        with self.assertRaises(IdempotencyConflict):
            self.positions.close_position(other, "c1", account="u1")
        self.assertEqual(self.positions.get_position(other)["status"], "active")

    def test_old_databases_are_migrated(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.sqlite3")
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE liquidity_positions (position_id TEXT PRIMARY KEY, account TEXT NOT NULL,
                  pool_id TEXT NOT NULL, principal_minor INTEGER NOT NULL, current_value_minor INTEGER NOT NULL,
                  accrued_fees_minor INTEGER NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE conversion_quotes (quote_id TEXT PRIMARY KEY, account TEXT NOT NULL,
                  source_currency TEXT NOT NULL, target_currency TEXT NOT NULL, source_amount_minor INTEGER NOT NULL,
                  target_amount_minor INTEGER NOT NULL, rate TEXT NOT NULL, fee_bps INTEGER NOT NULL,
                  expires_at REAL NOT NULL, consumed_by TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)
            old.close()
            ledger = WalletLedger(path)
            self.addCleanup(ledger.close)
            LiquidityPositions(ledger)
            ConversionQuotes(ledger)
            cols = {r["name"] for r in ledger.db.execute("PRAGMA table_info(liquidity_positions)")}
            self.assertTrue({"asset", "quote_asset", "realized_pnl_minor", "entry_price", "opened_ts"} <= cols)
            qcols = {r["name"] for r in ledger.db.execute("PRAGMA table_info(conversion_quotes)")}
            self.assertTrue({"fee_minor", "rate_source", "rate_as_of"} <= qcols)


class TestOwnershipAndLimits(unittest.TestCase):
    def setUp(self):
        self.ledger = WalletLedger()
        self.addCleanup(self.ledger.close)
        self.quotes = ConversionQuotes(self.ledger, Decimal("1500"), fee_bps=50, ttl_seconds=60, max_open_quotes=3)
        self.positions = LiquidityPositions(self.ledger)
        for user in ("alice", "mallory"):
            self.ledger.deposit(user, "NGN", 15_000_000, f"ngn-{user}")
            self.ledger.deposit(user, "USDT", 100_000_000, f"usdt-{user}")

    def test_quote_cannot_be_executed_by_another_account(self):
        quote = self.quotes.create_quote("alice", "NGN", 15_000_000)
        with self.assertRaises(QuoteError):
            self.quotes.execute(quote["quote_id"], "steal-1", account="mallory")
        self.assertEqual(self.ledger.balance("alice", "NGN"), 15_000_000)
        self.quotes.execute(quote["quote_id"], "ok-1", account="alice")     # the real owner is unaffected

    def test_idempotent_replay_is_bound_to_the_account(self):
        quote = self.quotes.create_quote("alice", "NGN", 15_000_000)
        self.quotes.execute(quote["quote_id"], "same-key", account="alice")
        with self.assertRaises(IdempotencyConflict):
            self.quotes.execute(quote["quote_id"], "same-key", account="mallory")

    def test_amount_limits(self):
        with self.assertRaises(QuoteError):
            self.quotes.create_quote("alice", "NGN", 100)
        with self.assertRaises(QuoteError):
            self.quotes.create_quote("alice", "NGN", 10 ** 12)

    def test_open_quote_cap(self):
        for _ in range(3):
            self.quotes.create_quote("alice", "NGN", 1_000_000)
        with self.assertRaises(QuoteError):
            self.quotes.create_quote("alice", "NGN", 1_000_000)
        self.quotes.create_quote("mallory", "NGN", 1_000_000)             # other accounts are unaffected

    def test_position_cannot_be_closed_or_read_by_another_account(self):
        pid = self.positions.open_position("alice", "pool", "A", "B", 10_000_000, "o1")["position_id"]
        with self.assertRaises(PositionNotFound):
            self.positions.close_position(pid, "c1", account="mallory")
        with self.assertRaises(PositionNotFound):
            self.positions.get_position(pid, account="mallory")
        self.assertEqual(self.positions.get_position(pid, account="alice")["status"], "active")
        self.assertEqual(self.positions.pool_exposure("pool"), 10_000_000)
        self.assertEqual(self.positions.count_open("alice"), 1)

    def test_close_retry_by_other_account_is_a_conflict(self):
        pid = self.positions.open_position("alice", "pool", "A", "B", 10_000_000, "o1")["position_id"]
        self.positions.close_position(pid, "c1", account="alice")
        with self.assertRaises(IdempotencyConflict):
            self.positions.close_position(pid, "c1", account="mallory")


if __name__ == "__main__":
    unittest.main()
