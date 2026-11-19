import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.ledger import (
    CurrencyLimits, IdempotencyConflict, InsufficientFunds, InvalidOperation, InvalidTransition,
    LedgerError, LimitExceeded, WalletLedger,
)

BANK = {"bank_code": "058", "account_number": "0123456789"}
TRON = {"network": "TRC20", "address": "T" + "A" * 33}
EVM = {"network": "ERC20", "address": "0x" + "ab" * 20}


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.ledger = WalletLedger()
        self.addCleanup(self.ledger.close)


class TestInvariants(LedgerCase):
    def test_unbalanced_operation_is_rejected(self):
        with self.assertRaises(LedgerError):
            with self.ledger.transaction():
                self.ledger._record_operation("op", "k", "test", {}, {}, [("u1", "USDT", 5, "x")])
        self.assertEqual(self.ledger.entries(), [])

    def test_customer_balance_cannot_go_negative(self):
        with self.assertRaises(InsufficientFunds):
            with self.ledger.transaction():
                self.ledger._record_operation("op", "k", "test", {}, {}, [
                    ("u1", "USDT", -5, "x"), ("clearing", "USDT", 5, "x")])
        self.assertEqual(self.ledger.balance("u1", "USDT"), 0)

    def test_system_account_names_are_reserved(self):
        for name in ("clearing", "fees", "withdrawal-hold", "liquidity-clearing"):
            with self.assertRaises(InvalidOperation):
                self.ledger.deposit(name, "USDT", 100, "k")

    def test_reconcile_clean_after_activity(self):
        self.ledger.deposit("u1", "USDT", 100_000_000, "d1")
        self.ledger.convert("u1", "u1", "USDT", "NGN", 1_000_000, 150_000, "c1")
        self.assertEqual(self.ledger.reconcile(), {"ok": True, "problems": []})

    def test_reconcile_detects_tampering_outside_the_api(self):
        self.ledger.db.execute(
            "INSERT INTO operations VALUES ('bad','bad','x','{}','{}',CURRENT_TIMESTAMP)")
        self.ledger.db.execute(
            "INSERT INTO ledger_entries(operation_id, account, currency, amount_minor) VALUES ('bad','u1','NGN',7)")
        report = self.ledger.reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("nets 7 NGN" in p for p in report["problems"]))

    def test_entries_are_immutable(self):
        self.ledger.deposit("u1", "USDT", 100, "d1")
        with self.assertRaises(Exception):
            self.ledger.db.execute("DELETE FROM ledger_entries")


class TestDeposits(LedgerCase):
    def test_pending_deposit_is_not_spendable_until_confirmed(self):
        self.ledger.register_deposit("u1", "NGN", 500_000, "ref-1")
        self.assertEqual(self.ledger.balance("u1", "NGN"), 0)
        self.ledger.confirm_deposit("ref-1")
        self.assertEqual(self.ledger.balance("u1", "NGN"), 500_000)

    def test_duplicate_webhook_credits_once(self):
        self.ledger.register_deposit("u1", "NGN", 500_000, "ref-1")
        self.ledger.confirm_deposit("ref-1")
        self.ledger.confirm_deposit("ref-1")
        self.ledger.register_deposit("u1", "NGN", 500_000, "ref-1")
        self.assertEqual(self.ledger.balance("u1", "NGN"), 500_000)

    def test_provider_ref_reused_for_different_deposit_is_rejected(self):
        self.ledger.register_deposit("u1", "NGN", 500_000, "ref-1")
        with self.assertRaises(IdempotencyConflict):
            self.ledger.register_deposit("u1", "NGN", 600_000, "ref-1")

    def test_rejected_deposit_cannot_be_confirmed(self):
        self.ledger.register_deposit("u1", "NGN", 500_000, "ref-1")
        self.ledger.reject_deposit("ref-1", "name mismatch")
        with self.assertRaises(InvalidTransition):
            self.ledger.confirm_deposit("ref-1")
        self.assertEqual(self.ledger.balance("u1", "NGN"), 0)

    def test_unknown_deposit(self):
        with self.assertRaises(InvalidOperation):
            self.ledger.confirm_deposit("nope")


class TestWithdrawals(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ledger.deposit("u1", "USDT", 100_000_000, "d-usdt")
        self.ledger.deposit("u1", "NGN", 50_000_000, "d-ngn")

    def test_request_reserves_amount_plus_fee(self):
        w = self.ledger.request_withdrawal("u1", "USDT", 10_000_000, TRON, "w1")
        self.assertEqual(w["status"], "pending")
        self.assertEqual(w["fee_minor"], 1_000_000)
        self.assertEqual(self.ledger.balance("u1", "USDT"), 89_000_000)
        self.assertEqual(self.ledger.balance("withdrawal-hold", "USDT"), 11_000_000)
        self.assertTrue(self.ledger.reconcile()["ok"])

    def test_full_lifecycle_books_payout_and_fee(self):
        w = self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        self.ledger.mark_withdrawal_processing(w["withdrawal_id"], "prov-1")
        done = self.ledger.complete_withdrawal(w["withdrawal_id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.ledger.balance("u1", "NGN"), 50_000_000 - 1_000_000 - 5_000)
        self.assertEqual(self.ledger.balance("fees", "NGN"), 5_000)
        self.assertEqual(self.ledger.balance("withdrawal-hold", "NGN"), 0)
        self.assertTrue(self.ledger.reconcile()["ok"])

    def test_complete_requires_processing(self):
        w = self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        with self.assertRaises(InvalidTransition):
            self.ledger.complete_withdrawal(w["withdrawal_id"])

    def test_failure_returns_amount_and_fee(self):
        w = self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        self.ledger.mark_withdrawal_processing(w["withdrawal_id"], "prov-1")
        failed = self.ledger.fail_withdrawal(w["withdrawal_id"], "bank rejected")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.ledger.balance("u1", "NGN"), 50_000_000)
        self.assertTrue(self.ledger.reconcile()["ok"])

    def test_cancel_only_before_provider_accepts(self):
        w = self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        self.ledger.mark_withdrawal_processing(w["withdrawal_id"], "prov-1")
        with self.assertRaises(InvalidTransition):
            self.ledger.cancel_withdrawal(w["withdrawal_id"])
        w2 = self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w2")
        self.ledger.cancel_withdrawal(w2["withdrawal_id"])
        self.assertEqual(self.ledger.get_withdrawal(w2["withdrawal_id"])["status"], "cancelled")

    def test_finished_withdrawal_cannot_change_again(self):
        w = self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        self.ledger.mark_withdrawal_processing(w["withdrawal_id"], "prov-1")
        self.ledger.complete_withdrawal(w["withdrawal_id"])
        with self.assertRaises(InvalidTransition):
            self.ledger.fail_withdrawal(w["withdrawal_id"])

    def test_retries_do_not_double_hold_or_double_pay(self):
        a = self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        b = self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        self.assertEqual(a["withdrawal_id"], b["withdrawal_id"])
        self.assertEqual(self.ledger.balance("u1", "NGN"), 50_000_000 - 1_005_000)
        self.ledger.mark_withdrawal_processing(a["withdrawal_id"], "prov-1")
        self.ledger.complete_withdrawal(a["withdrawal_id"])
        self.ledger.complete_withdrawal(a["withdrawal_id"])
        self.assertEqual(self.ledger.balance("fees", "NGN"), 5_000)

    def test_same_key_different_request_rejected(self):
        self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        with self.assertRaises(IdempotencyConflict):
            self.ledger.request_withdrawal("u1", "NGN", 2_000_000, BANK, "w1")

    def test_insufficient_funds_including_fee(self):
        with self.assertRaises(InsufficientFunds):
            self.ledger.request_withdrawal("u1", "USDT", 100_000_000, TRON, "w1")
        self.assertEqual(self.ledger.balance("u1", "USDT"), 100_000_000)

    def test_amount_limits(self):
        with self.assertRaises(LimitExceeded):
            self.ledger.request_withdrawal("u1", "NGN", 100, BANK, "tiny")
        limits = {"NGN": CurrencyLimits(10_000, 2_000_000, 3_000_000, 0)}
        ledger = WalletLedger(withdrawal_limits=limits)
        self.addCleanup(ledger.close)
        ledger.deposit("u1", "NGN", 50_000_000, "d")
        with self.assertRaises(LimitExceeded):
            ledger.request_withdrawal("u1", "NGN", 2_500_000, BANK, "big")
        ledger.request_withdrawal("u1", "NGN", 2_000_000, BANK, "ok1")
        with self.assertRaises(LimitExceeded):      # 2.0M + 1.5M exceeds the 3.0M daily cap
            ledger.request_withdrawal("u1", "NGN", 1_500_000, BANK, "ok2")

    def test_failed_withdrawals_do_not_count_against_daily_cap(self):
        limits = {"NGN": CurrencyLimits(10_000, 2_000_000, 2_000_000, 0)}
        ledger = WalletLedger(withdrawal_limits=limits)
        self.addCleanup(ledger.close)
        ledger.deposit("u1", "NGN", 50_000_000, "d")
        w = ledger.request_withdrawal("u1", "NGN", 2_000_000, BANK, "a")
        ledger.fail_withdrawal(w["withdrawal_id"])
        ledger.request_withdrawal("u1", "NGN", 2_000_000, BANK, "b")

    def test_destination_validation(self):
        bad = [
            ("NGN", {"bank_code": "058", "account_number": "123"}),
            ("NGN", {"account_number": "0123456789"}),
            ("USDT", {"network": "TRC20", "address": "0x" + "ab" * 20}),
            ("USDT", {"network": "ERC20", "address": "T" + "A" * 33}),
            ("USDT", {"network": "DOGE", "address": "x"}),
            ("USDT", "0xabc"),
        ]
        for currency, destination in bad:
            with self.assertRaises(InvalidOperation, msg=str(destination)):
                self.ledger.request_withdrawal("u1", currency, 10_000_000, destination, f"k-{currency}-{destination}")
        self.ledger.request_withdrawal("u1", "USDT", 10_000_000, EVM, "good")

    def test_list_and_filter(self):
        self.ledger.request_withdrawal("u1", "NGN", 1_000_000, BANK, "w1")
        self.assertEqual(len(self.ledger.list_withdrawals("u1")), 1)
        self.assertEqual(self.ledger.list_withdrawals("u1", status="completed"), [])


class TestWithdrawalMigration(unittest.TestCase):
    def test_legacy_withdrawals_and_index_survive_submission_status_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"

            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE withdrawals (
                    withdrawal_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL CHECK(currency IN ('NGN', 'USDT')),
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    fee_minor INTEGER NOT NULL CHECK(fee_minor >= 0),
                    destination_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending', 'processing', 'completed', 'failed', 'cancelled')),
                    provider_ref TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_withdrawals_account
                    ON withdrawals(account, currency, status);
                INSERT INTO withdrawals (
                    withdrawal_id, account, currency, amount_minor, fee_minor,
                    destination_json, status, provider_ref, reason, idempotency_key
                ) VALUES (
                    'wd:legacy-1', 'u1', 'NGN', 100000, 5000,
                    '{"account_number":"0123456789","bank_code":"058"}',
                    'pending', NULL, '', 'legacy-1'
                );
            """)
            connection.close()

            migrated = WalletLedger(database)
            self.addCleanup(migrated.close)

            row = migrated.get_withdrawal("wd:legacy-1")
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["amount_minor"], 100000)
            self.assertEqual(row["fee_minor"], 5000)
            self.assertEqual(row["idempotency_key"], "legacy-1")

            index = migrated.db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_withdrawals_account'"
            ).fetchone()
            self.assertIsNotNone(index)

            claimed, updated = migrated.claim_withdrawal_submission("wd:legacy-1")
            self.assertTrue(claimed)
            self.assertEqual(updated["status"], "submitting")


if __name__ == "__main__":
    unittest.main()
