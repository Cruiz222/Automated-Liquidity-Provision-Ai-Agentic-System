"""Append-only simulated wallet ledger for NGN and USDT.

NGN amounts are stored in kobo; USDT amounts are stored in micro-USDT.
This module is local simulation infrastructure only: it does not connect to
banks, payment providers, wallets, or blockchains.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SUPPORTED_CURRENCIES = {"NGN", "USDT"}


class LedgerError(Exception):
    """Base class for wallet-ledger errors."""


class InvalidOperation(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    pass


def _positive_int(value: int, field: str = "amount") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidOperation(f"{field} must be a positive integer in minor units")
    return value


class WalletLedger:
    """SQLite-backed wallet balances derived from immutable ledger entries."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.database, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    operation_type TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL CHECK(currency IN ('NGN', 'USDT')),
                    amount_minor INTEGER NOT NULL CHECK(amount_minor != 0),
                    memo TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_account_currency
                    ON ledger_entries(account, currency, entry_id);
                CREATE TRIGGER IF NOT EXISTS ledger_entries_no_update
                    BEFORE UPDATE ON ledger_entries
                    BEGIN SELECT RAISE(ABORT, 'ledger entries are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS ledger_entries_no_delete
                    BEFORE DELETE ON ledger_entries
                    BEGIN SELECT RAISE(ABORT, 'ledger entries are immutable'); END;
            """)

    def close(self) -> None:
        with self._lock:
            self.db.close()

    @staticmethod
    def _validate_currency(currency: str) -> str:
        if currency not in SUPPORTED_CURRENCIES:
            raise InvalidOperation(f"unsupported currency: {currency!r}")
        return currency

    def balance(self, account: str, currency: str) -> int:
        if not account or not isinstance(account, str):
            raise InvalidOperation("account must be a non-empty string")
        self._validate_currency(currency)
        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(SUM(amount_minor), 0) AS balance FROM ledger_entries "
                "WHERE account = ? AND currency = ?", (account, currency)
            ).fetchone()
            return int(row["balance"])

    def balances(self, account: str) -> dict[str, int]:
        return {currency: self.balance(account, currency) for currency in sorted(SUPPORTED_CURRENCIES)}

    def entries(self, account: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if account is None:
                rows = self.db.execute("SELECT * FROM ledger_entries ORDER BY entry_id").fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM ledger_entries WHERE account = ? ORDER BY entry_id", (account,)
                ).fetchall()
            return [dict(row) for row in rows]

    def _existing_operation(self, idempotency_key: str, request: dict[str, Any]) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT request_json, result_json FROM operations WHERE idempotency_key = ?",
            (idempotency_key,)
        ).fetchone()
        if row is None:
            return None
        if row["request_json"] != json.dumps(request, sort_keys=True, separators=(",", ":")):
            raise IdempotencyConflict("idempotency key was already used for a different request")
        return json.loads(row["result_json"])

    def _record_operation(
        self, operation_id: str, idempotency_key: str, operation_type: str,
        request: dict[str, Any], result: dict[str, Any], entries: list[tuple[str, str, int, str]]
    ) -> None:
        self.db.execute(
            "INSERT INTO operations(operation_id, idempotency_key, operation_type, request_json, result_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (operation_id, idempotency_key, operation_type,
             json.dumps(request, sort_keys=True, separators=(",", ":")),
             json.dumps(result, sort_keys=True, separators=(",", ":")))
        )
        self.db.executemany(
            "INSERT INTO ledger_entries(operation_id, account, currency, amount_minor, memo) "
            "VALUES (?, ?, ?, ?, ?)",
            [(operation_id, account, currency, amount, memo)
             for account, currency, amount, memo in entries]
        )

    def deposit(self, account: str, currency: str, amount_minor: int, idempotency_key: str) -> dict[str, Any]:
        self._validate_currency(currency)
        _positive_int(amount_minor)
        if not account or not idempotency_key:
            raise InvalidOperation("account and idempotency_key are required")
        request = {"account": account, "currency": currency, "amount_minor": amount_minor}
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._existing_operation(idempotency_key, request)
                if existing is not None:
                    self.db.execute("COMMIT")
                    return existing
                op_id = f"deposit:{idempotency_key}"
                result = {"operation_id": op_id, "account": account, "currency": currency,
                          "amount_minor": amount_minor, "status": "simulated"}
                self._record_operation(op_id, idempotency_key, "deposit", request, result, [
                    (account, currency, amount_minor, "simulated deposit"),
                    ("clearing", currency, -amount_minor, "simulated deposit clearing"),
                ])
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def withdraw(self, account: str, currency: str, amount_minor: int, idempotency_key: str) -> dict[str, Any]:
        """Record a simulated withdrawal to clearing; no bank/on-chain payout occurs."""
        self._validate_currency(currency)
        _positive_int(amount_minor)
        if not account or not idempotency_key:
            raise InvalidOperation("account and idempotency_key are required")
        request = {"account": account, "currency": currency, "amount_minor": amount_minor}
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._existing_operation(idempotency_key, request)
                if existing is not None:
                    self.db.execute("COMMIT")
                    return existing
                available = self.balance(account, currency)
                if available < amount_minor:
                    raise InsufficientFunds(f"insufficient {currency}: have {available}, need {amount_minor}")
                op_id = f"withdraw:{idempotency_key}"
                result = {"operation_id": op_id, "account": account, "currency": currency,
                          "amount_minor": amount_minor, "status": "simulated"}
                self._record_operation(op_id, idempotency_key, "withdrawal", request, result, [
                    (account, currency, -amount_minor, "simulated withdrawal"),
                    ("clearing", currency, amount_minor, "simulated withdrawal clearing"),
                ])
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def convert(self, source_account: str, target_account: str, source_currency: str,
                target_currency: str, source_amount_minor: int, target_amount_minor: int,
                idempotency_key: str) -> dict[str, Any]:
        self._validate_currency(source_currency)
        self._validate_currency(target_currency)
        _positive_int(source_amount_minor, "source_amount_minor")
        _positive_int(target_amount_minor, "target_amount_minor")
        if source_currency == target_currency:
            raise InvalidOperation("conversion currencies must differ")
        if not source_account or not target_account or not idempotency_key:
            raise InvalidOperation("accounts and idempotency_key are required")
        request = {"source_account": source_account, "target_account": target_account,
                   "source_currency": source_currency, "target_currency": target_currency,
                   "source_amount_minor": source_amount_minor, "target_amount_minor": target_amount_minor}
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._existing_operation(idempotency_key, request)
                if existing is not None:
                    self.db.execute("COMMIT")
                    return existing
                available = self.balance(source_account, source_currency)
                if available < source_amount_minor:
                    raise InsufficientFunds(
                        f"insufficient {source_currency}: have {available}, need {source_amount_minor}"
                    )
                op_id = f"convert:{idempotency_key}"
                result = {"operation_id": op_id, "source_account": source_account,
                          "target_account": target_account, "source_currency": source_currency,
                          "target_currency": target_currency, "source_amount_minor": source_amount_minor,
                          "target_amount_minor": target_amount_minor, "status": "simulated"}
                self._record_operation(op_id, idempotency_key, "convert", request, result, [
                    (source_account, source_currency, -source_amount_minor, "conversion source debit"),
                    ("clearing", source_currency, source_amount_minor, "conversion source clearing"),
                    ("clearing", target_currency, -target_amount_minor, "conversion target clearing"),
                    (target_account, target_currency, target_amount_minor, "conversion target credit"),
                ])
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise
