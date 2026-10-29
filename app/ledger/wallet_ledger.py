"""Append-only simulated wallet ledger for NGN and USDT.

NGN amounts are stored in kobo; USDT amounts are stored in micro-USDT.
This module is local simulation infrastructure: it does not connect to banks,
payment providers, wallets, or blockchains.

Guarantees enforced by the ledger itself:

* every operation nets to zero per currency (double entry);
* entries are immutable (database triggers);
* customer accounts can never go negative; system accounts may;
* operations are idempotent per idempotency key, and a key reused for a
  different request is rejected.

System accounts: ``clearing`` (the outside world), ``liquidity-clearing``
(simulated pool deposits), ``withdrawal-hold`` (funds reserved for pending
withdrawals) and ``fees`` (platform revenue).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SUPPORTED_CURRENCIES = {"NGN", "USDT"}
SYSTEM_ACCOUNTS = frozenset({"clearing", "liquidity-clearing", "withdrawal-hold", "fees"})


class LedgerError(Exception):
    """Base class for wallet-ledger errors."""


class InvalidOperation(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    pass


class InvalidTransition(LedgerError):
    """A deposit or withdrawal was moved to a state its current state does not allow."""


class LimitExceeded(LedgerError):
    """A withdrawal violated a configured amount or daily limit."""


@dataclass(frozen=True)
class CurrencyLimits:
    min_amount_minor: int
    max_amount_minor: int
    daily_cap_minor: int
    fee_minor: int


# Placeholder policy: tune before real use. NGN in kobo, USDT in micro-USDT.
DEFAULT_WITHDRAWAL_LIMITS = {
    "NGN": CurrencyLimits(min_amount_minor=10_000, max_amount_minor=500_000_000,
                          daily_cap_minor=1_000_000_000, fee_minor=5_000),
    "USDT": CurrencyLimits(min_amount_minor=5_000_000, max_amount_minor=5_000_000_000,
                           daily_cap_minor=10_000_000_000, fee_minor=1_000_000),
}

_EVM_NETWORKS = {"ERC20", "BEP20", "POLYGON", "ARBITRUM", "BASE", "OPTIMISM"}
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRON_ADDRESS = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


def ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Add columns missing from a table created by an earlier version (internal constant names only)."""
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _positive_int(value: int, field: str = "amount") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidOperation(f"{field} must be a positive integer in minor units")
    return value


def _validate_destination(currency: str, destination: Any) -> dict[str, str]:
    """Format-check a payout destination. This is not an ownership or checksum check."""
    if not isinstance(destination, dict):
        raise InvalidOperation("destination must be an object")
    if currency == "NGN":
        bank_code = destination.get("bank_code")
        number = destination.get("account_number")
        if not isinstance(number, str) or not re.fullmatch(r"\d{10}", number):
            raise InvalidOperation("NGN destination needs a 10-digit account_number")
        if not isinstance(bank_code, str) or not re.fullmatch(r"\d{3,6}", bank_code):
            raise InvalidOperation("NGN destination needs a numeric bank_code")
        return {"bank_code": bank_code, "account_number": number}
    network = str(destination.get("network", "")).upper()
    address = destination.get("address")
    if not isinstance(address, str):
        raise InvalidOperation("USDT destination needs an address")
    if network in _EVM_NETWORKS:
        if not _EVM_ADDRESS.fullmatch(address):
            raise InvalidOperation(f"invalid {network} address")
    elif network == "TRC20":
        if not _TRON_ADDRESS.fullmatch(address):
            raise InvalidOperation("invalid TRC20 address")
    else:
        raise InvalidOperation(f"unsupported USDT network: {network!r}")
    return {"network": network, "address": address}


class WalletLedger:
    """SQLite-backed wallet balances derived from immutable ledger entries."""

    def __init__(self, database: str | Path = ":memory:",
                 withdrawal_limits: dict[str, CurrencyLimits] | None = None) -> None:
        self.database = str(database)
        self.withdrawal_limits = dict(DEFAULT_WITHDRAWAL_LIMITS)
        for currency, limits in (withdrawal_limits or {}).items():
            if currency not in SUPPORTED_CURRENCIES:
                raise InvalidOperation(f"unsupported currency: {currency!r}")
            self.withdrawal_limits[currency] = limits
        self._lock = threading.RLock()
        self.lock = self._lock          # shared by services that use this connection
        self.db = sqlite3.connect(self.database, check_same_thread=False, isolation_level=None)
        if self.database != ":memory:":
            try:
                Path(self.database).chmod(0o600)        # owner-only: the file holds balances, hashes and 2FA secrets
            except OSError:
                pass
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
                CREATE TABLE IF NOT EXISTS deposits (
                    provider_ref TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL CHECK(currency IN ('NGN', 'USDT')),
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'rejected')),
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS withdrawals (
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
                CREATE INDEX IF NOT EXISTS idx_withdrawals_account
                    ON withdrawals(account, currency, status);
            """)

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ---- infrastructure -------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block inside one ``BEGIN IMMEDIATE`` transaction (commit or roll back)."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    _tx = transaction

    @staticmethod
    def _validate_currency(currency: str) -> str:
        if currency not in SUPPORTED_CURRENCIES:
            raise InvalidOperation(f"unsupported currency: {currency!r}")
        return currency

    @staticmethod
    def _check_user_account(account: Any) -> str:
        if not account or not isinstance(account, str):
            raise InvalidOperation("account must be a non-empty string")
        if account in SYSTEM_ACCOUNTS:
            raise InvalidOperation(f"{account!r} is a reserved system account")
        return account

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
        """Insert an operation and its entries. Call inside ``transaction()``.

        Enforces per-currency zero-sum and non-negative customer balances; the
        caller's transaction rolls back if either fails.
        """
        totals: dict[str, int] = {}
        for _account, currency, amount, _memo in entries:
            self._validate_currency(currency)
            if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
                raise InvalidOperation("ledger entry amounts must be non-zero integers")
            totals[currency] = totals.get(currency, 0) + amount
        if any(total != 0 for total in totals.values()):
            raise LedgerError("operation entries must net to zero in every currency")
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
        for account, currency in {(a, c) for a, c, _, _ in entries if a not in SYSTEM_ACCOUNTS}:
            balance = self.balance(account, currency)
            if balance < 0:
                raise InsufficientFunds(f"{account} would have negative {currency} balance ({balance})")

    # ---- reads ------------------------------------------------------------

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

    def history(self, account: str, limit: int = 50, before_entry_id: int | None = None) -> list[dict[str, Any]]:
        """Newest-first entries for one account with their operation type (for statements)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise InvalidOperation("limit must be an integer from 1 to 200")
        params: list[Any] = [account]
        clause = ""
        if before_entry_id is not None:
            if isinstance(before_entry_id, bool) or not isinstance(before_entry_id, int) or before_entry_id < 1:
                raise InvalidOperation("before must be a positive integer")
            clause = " AND e.entry_id < ?"
            params.append(before_entry_id)
        params.append(limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT e.entry_id, e.created_at, o.operation_type, e.currency, e.amount_minor, e.memo "
                "FROM ledger_entries e JOIN operations o ON o.operation_id = e.operation_id "
                "WHERE e.account = ?" + clause + " ORDER BY e.entry_id DESC LIMIT ?", params).fetchall()
            return [dict(row) for row in rows]

    def daily_total(self, account: str, currency: str, memo: str) -> int:
        """Sum of today's (UTC) positive entries with this memo, used for daily caps."""
        self._validate_currency(currency)
        with self._lock:
            return int(self.db.execute(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries WHERE account = ? AND currency = ? "
                "AND memo = ? AND amount_minor > 0 AND date(created_at) = date('now')",
                (account, currency, memo)).fetchone()[0])

    def reconcile(self) -> dict[str, Any]:
        """Check ledger invariants. ``ok`` is False if any problem is listed."""
        problems: list[str] = []
        with self._lock:
            for row in self.db.execute(
                "SELECT operation_id, currency, SUM(amount_minor) AS net FROM ledger_entries "
                "GROUP BY operation_id, currency HAVING net != 0"
            ).fetchall():
                problems.append(f"operation {row['operation_id']} nets {row['net']} {row['currency']}")
            for row in self.db.execute(
                "SELECT account, currency, SUM(amount_minor) AS bal FROM ledger_entries "
                "GROUP BY account, currency"
            ).fetchall():
                if row["account"] not in SYSTEM_ACCOUNTS and row["bal"] < 0:
                    problems.append(f"{row['account']} has negative {row['currency']} balance {row['bal']}")
            for currency in sorted(SUPPORTED_CURRENCIES):
                held = self.balance("withdrawal-hold", currency)
                expected = self.db.execute(
                    "SELECT COALESCE(SUM(amount_minor + fee_minor), 0) FROM withdrawals "
                    "WHERE currency = ? AND status IN ('pending', 'processing')", (currency,)
                ).fetchone()[0]
                if held != expected:
                    problems.append(f"withdrawal-hold {currency} is {held}, open withdrawals total {expected}")
        return {"ok": not problems, "problems": problems}

    # ---- deposits ---------------------------------------------------------

    def deposit(self, account: str, currency: str, amount_minor: int, idempotency_key: str) -> dict[str, Any]:
        """Credit funds immediately (simulation shortcut; see register/confirm for the provider flow)."""
        self._check_user_account(account)
        self._validate_currency(currency)
        _positive_int(amount_minor)
        if not idempotency_key:
            raise InvalidOperation("account and idempotency_key are required")
        request = {"account": account, "currency": currency, "amount_minor": amount_minor}
        with self.transaction():
            existing = self._existing_operation(idempotency_key, request)
            if existing is not None:
                return existing
            op_id = f"deposit:{idempotency_key}"
            result = {"operation_id": op_id, "account": account, "currency": currency,
                      "amount_minor": amount_minor, "status": "simulated"}
            self._record_operation(op_id, idempotency_key, "deposit", request, result, [
                (account, currency, amount_minor, "simulated deposit"),
                ("clearing", currency, -amount_minor, "simulated deposit clearing"),
            ])
            return result

    def register_deposit(self, account: str, currency: str, amount_minor: int, provider_ref: str) -> dict[str, Any]:
        """Record an expected deposit as pending. Funds are not usable until confirmed."""
        self._check_user_account(account)
        self._validate_currency(currency)
        _positive_int(amount_minor)
        if not provider_ref or not isinstance(provider_ref, str):
            raise InvalidOperation("provider_ref is required")
        with self.transaction():
            row = self.db.execute("SELECT * FROM deposits WHERE provider_ref = ?", (provider_ref,)).fetchone()
            if row is not None:
                if (row["account"], row["currency"], row["amount_minor"]) != (account, currency, amount_minor):
                    raise IdempotencyConflict("provider_ref was already registered for a different deposit")
                return dict(row)
            self.db.execute(
                "INSERT INTO deposits(provider_ref, account, currency, amount_minor, status) "
                "VALUES (?, ?, ?, ?, 'pending')", (provider_ref, account, currency, amount_minor))
            return dict(self.db.execute("SELECT * FROM deposits WHERE provider_ref = ?", (provider_ref,)).fetchone())

    def confirm_deposit(self, provider_ref: str) -> dict[str, Any]:
        """Credit a pending deposit exactly once (safe against duplicate provider webhooks)."""
        with self.transaction():
            row = self.db.execute("SELECT * FROM deposits WHERE provider_ref = ?", (provider_ref,)).fetchone()
            if row is None:
                raise InvalidOperation("unknown deposit")
            if row["status"] == "confirmed":
                return dict(row)
            if row["status"] != "pending":
                raise InvalidTransition(f"deposit is {row['status']}")
            key = f"deposit-confirm:{provider_ref}"
            request = {"provider_ref": provider_ref}
            result = {"provider_ref": provider_ref, "account": row["account"],
                      "currency": row["currency"], "amount_minor": row["amount_minor"], "status": "confirmed"}
            self._record_operation(key, key, "deposit_confirm", request, result, [
                (row["account"], row["currency"], row["amount_minor"], "confirmed deposit"),
                ("clearing", row["currency"], -row["amount_minor"], "confirmed deposit clearing"),
            ])
            self.db.execute("UPDATE deposits SET status = 'confirmed', updated_at = CURRENT_TIMESTAMP "
                            "WHERE provider_ref = ?", (provider_ref,))
            return dict(self.db.execute("SELECT * FROM deposits WHERE provider_ref = ?", (provider_ref,)).fetchone())

    def reject_deposit(self, provider_ref: str, reason: str = "") -> dict[str, Any]:
        with self.transaction():
            row = self.db.execute("SELECT * FROM deposits WHERE provider_ref = ?", (provider_ref,)).fetchone()
            if row is None:
                raise InvalidOperation("unknown deposit")
            if row["status"] == "rejected":
                return dict(row)
            if row["status"] != "pending":
                raise InvalidTransition(f"deposit is {row['status']}")
            self.db.execute("UPDATE deposits SET status = 'rejected', reason = ?, updated_at = CURRENT_TIMESTAMP "
                            "WHERE provider_ref = ?", (reason, provider_ref))
            return dict(self.db.execute("SELECT * FROM deposits WHERE provider_ref = ?", (provider_ref,)).fetchone())

    # ---- conversion -------------------------------------------------------

    def convert(self, source_account: str, target_account: str, source_currency: str,
                target_currency: str, source_amount_minor: int, target_amount_minor: int,
                idempotency_key: str) -> dict[str, Any]:
        self._validate_currency(source_currency)
        self._validate_currency(target_currency)
        _positive_int(source_amount_minor, "source_amount_minor")
        _positive_int(target_amount_minor, "target_amount_minor")
        if source_currency == target_currency:
            raise InvalidOperation("conversion currencies must differ")
        if not idempotency_key:
            raise InvalidOperation("accounts and idempotency_key are required")
        self._check_user_account(source_account)
        self._check_user_account(target_account)
        request = {"source_account": source_account, "target_account": target_account,
                   "source_currency": source_currency, "target_currency": target_currency,
                   "source_amount_minor": source_amount_minor, "target_amount_minor": target_amount_minor}
        with self.transaction():
            existing = self._existing_operation(idempotency_key, request)
            if existing is not None:
                return existing
            available = self.balance(source_account, source_currency)
            if available < source_amount_minor:
                raise InsufficientFunds(
                    f"insufficient {source_currency}: have {available}, need {source_amount_minor}"
                )
            op_id = f"convert:{idempotency_key}"
            result = {"operation_id": op_id, **request, "status": "simulated"}
            self._record_operation(op_id, idempotency_key, "convert", request, result, [
                (source_account, source_currency, -source_amount_minor, "conversion source debit"),
                ("clearing", source_currency, source_amount_minor, "conversion source clearing"),
                ("clearing", target_currency, -target_amount_minor, "conversion target clearing"),
                (target_account, target_currency, target_amount_minor, "conversion target credit"),
            ])
            return result

    # ---- withdrawals ------------------------------------------------------

    @staticmethod
    def _withdrawal_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["destination"] = json.loads(data.pop("destination_json"))
        return data

    def get_withdrawal(self, withdrawal_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM withdrawals WHERE withdrawal_id = ?", (withdrawal_id,)).fetchone()
            if row is None:
                raise InvalidOperation("unknown withdrawal")
            return self._withdrawal_dict(row)

    def list_withdrawals(self, account: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM withdrawals", []
        clauses = []
        if account is not None:
            clauses.append("account = ?")
            params.append(account)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self.db.execute(query + " ORDER BY created_at, withdrawal_id", params).fetchall()
            return [self._withdrawal_dict(row) for row in rows]

    def request_withdrawal(self, account: str, currency: str, amount_minor: int,
                           destination: dict[str, str], idempotency_key: str,
                           limits: CurrencyLimits | None = None) -> dict[str, Any]:
        """Reserve funds (amount plus fee) for a payout; the funds leave the usable balance at once."""
        self._check_user_account(account)
        self._validate_currency(currency)
        _positive_int(amount_minor)
        if not idempotency_key or not isinstance(idempotency_key, str):
            raise InvalidOperation("idempotency_key is required")
        dest = _validate_destination(currency, destination)
        limits = limits or self.withdrawal_limits[currency]
        request = {"account": account, "currency": currency, "amount_minor": amount_minor, "destination": dest}
        with self.transaction():
            existing = self._existing_operation(idempotency_key, request)
            if existing is not None:
                return self.get_withdrawal(existing["withdrawal_id"])
            if amount_minor < limits.min_amount_minor:
                raise LimitExceeded(f"minimum withdrawal is {limits.min_amount_minor} {currency} minor units")
            if amount_minor > limits.max_amount_minor:
                raise LimitExceeded(f"maximum withdrawal is {limits.max_amount_minor} {currency} minor units")
            used = self.db.execute(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM withdrawals WHERE account = ? AND currency = ? "
                "AND status IN ('pending', 'processing', 'completed') AND date(created_at) = date('now')",
                (account, currency)).fetchone()[0]
            if used + amount_minor > limits.daily_cap_minor:
                raise LimitExceeded(f"daily {currency} withdrawal cap of {limits.daily_cap_minor} would be exceeded")
            total = amount_minor + limits.fee_minor
            available = self.balance(account, currency)
            if available < total:
                raise InsufficientFunds(f"insufficient {currency}: have {available}, need {total} including fee")
            withdrawal_id = f"wd:{idempotency_key}"
            op_id = f"withdrawal-request:{idempotency_key}"
            result = {"withdrawal_id": withdrawal_id, "status": "pending"}
            self._record_operation(op_id, idempotency_key, "withdrawal_request", request, result, [
                (account, currency, -total, "withdrawal hold"),
                ("withdrawal-hold", currency, total, "withdrawal hold"),
            ])
            self.db.execute(
                "INSERT INTO withdrawals(withdrawal_id, account, currency, amount_minor, fee_minor, "
                "destination_json, status, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (withdrawal_id, account, currency, amount_minor, limits.fee_minor,
                 json.dumps(dest, sort_keys=True), idempotency_key))
            return self.get_withdrawal(withdrawal_id)

    def mark_withdrawal_processing(self, withdrawal_id: str, provider_ref: str) -> dict[str, Any]:
        """Record that the payout provider accepted the payout (required before completion)."""
        if not provider_ref or not isinstance(provider_ref, str):
            raise InvalidOperation("provider_ref is required")
        with self.transaction():
            row = self.db.execute("SELECT * FROM withdrawals WHERE withdrawal_id = ?", (withdrawal_id,)).fetchone()
            if row is None:
                raise InvalidOperation("unknown withdrawal")
            if row["status"] == "processing" and row["provider_ref"] == provider_ref:
                return self._withdrawal_dict(row)
            if row["status"] != "pending":
                raise InvalidTransition(f"withdrawal is {row['status']}")
            self.db.execute("UPDATE withdrawals SET status = 'processing', provider_ref = ?, "
                            "updated_at = CURRENT_TIMESTAMP WHERE withdrawal_id = ?", (provider_ref, withdrawal_id))
        return self.get_withdrawal(withdrawal_id)

    def _finish_withdrawal(self, withdrawal_id: str, kind: str, allowed_from: tuple[str, ...],
                           new_status: str, reason: str = "") -> dict[str, Any]:
        key = f"withdrawal-{kind}:{withdrawal_id}"
        request = {"withdrawal_id": withdrawal_id}
        with self.transaction():
            row = self.db.execute("SELECT * FROM withdrawals WHERE withdrawal_id = ?", (withdrawal_id,)).fetchone()
            if row is None:
                raise InvalidOperation("unknown withdrawal")
            if self._existing_operation(key, request) is not None:
                return self._withdrawal_dict(row)
            if row["status"] not in allowed_from:
                raise InvalidTransition(f"cannot {kind} a withdrawal that is {row['status']}")
            currency, amount, fee = row["currency"], row["amount_minor"], row["fee_minor"]
            total = amount + fee
            if kind == "complete":
                entries = [("withdrawal-hold", currency, -total, "withdrawal payout"),
                           ("clearing", currency, amount, "withdrawal payout")]
                if fee:
                    entries.append(("fees", currency, fee, "withdrawal fee"))
            else:
                entries = [("withdrawal-hold", currency, -total, "withdrawal released"),
                           (row["account"], currency, total, "withdrawal returned")]
            self._record_operation(key, key, f"withdrawal_{kind}", request,
                                   {"withdrawal_id": withdrawal_id, "status": new_status}, entries)
            self.db.execute("UPDATE withdrawals SET status = ?, reason = ?, updated_at = CURRENT_TIMESTAMP "
                            "WHERE withdrawal_id = ?", (new_status, reason, withdrawal_id))
        return self.get_withdrawal(withdrawal_id)

    def complete_withdrawal(self, withdrawal_id: str) -> dict[str, Any]:
        """Pay out: releases the hold, sends the amount out, books the fee. Needs 'processing'."""
        return self._finish_withdrawal(withdrawal_id, "complete", ("processing",), "completed")

    def fail_withdrawal(self, withdrawal_id: str, reason: str = "") -> dict[str, Any]:
        """The provider failed the payout: return amount and fee to the customer."""
        return self._finish_withdrawal(withdrawal_id, "fail", ("pending", "processing"), "failed", reason)

    def cancel_withdrawal(self, withdrawal_id: str, reason: str = "") -> dict[str, Any]:
        """Customer cancellation, allowed only before the provider has accepted the payout."""
        return self._finish_withdrawal(withdrawal_id, "cancel", ("pending",), "cancelled", reason)
