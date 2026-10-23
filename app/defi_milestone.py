"""Simulated NGN/USDT conversion quotes and liquidity positions.

No bank, exchange, wallet, or blockchain integration is implemented. Quote rates
and position valuations are supplied by the caller and must not be treated as live.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from .wallet_ledger import (
    InsufficientFunds, InvalidOperation as LedgerInvalidOperation, WalletLedger,
)

class QuoteError(Exception):
    pass

class QuoteExpired(QuoteError):
    pass

class QuoteAlreadyUsed(QuoteError):
    pass

class ConversionQuotes:
    """Creates short-lived conversion quotes and executes each at most once."""

    def __init__(self, ledger: WalletLedger, ngn_per_usdt: Decimal | str | int | float = Decimal("1500"),
                 fee_bps: int = 50, ttl_seconds: int = 30):
        if isinstance(ngn_per_usdt, bool) or not isinstance(ngn_per_usdt, (Decimal, str, int, float)):
            from .wallet_ledger import InvalidOperation
            raise InvalidOperation("ngn_per_usdt must be a valid decimal")
        try:
            self.ngn_per_usdt = Decimal(str(ngn_per_usdt))
        except (InvalidOperation, ValueError):
            from .wallet_ledger import InvalidOperation
            raise InvalidOperation("ngn_per_usdt must be a valid decimal")
        if self.ngn_per_usdt <= 0:
            from .wallet_ledger import InvalidOperation
            raise InvalidOperation("ngn_per_usdt must be positive")
        if isinstance(fee_bps, bool) or not isinstance(fee_bps, int) or not 0 <= fee_bps < 10_000:
            raise ValueError("fee_bps must be an integer from 0 to 9999")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        self.ledger = ledger
        self.fee_bps = fee_bps
        self.ttl_seconds = ttl_seconds
        self.db = ledger.db
        self._lock = ledger._lock
        with self._lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS conversion_quotes (
                    quote_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    source_currency TEXT NOT NULL,
                    target_currency TEXT NOT NULL,
                    source_amount_minor INTEGER NOT NULL,
                    target_amount_minor INTEGER NOT NULL,
                    rate TEXT NOT NULL,
                    fee_bps INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_by TEXT UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def create_quote(self, account: str, source_currency: str, source_amount_minor: int,
                     target_currency: str | None = None,
                     rate: Decimal | str | int | float | None = None,
                     now: float | None = None) -> dict[str, Any]:
        if not account:
            raise QuoteError("account is required")
        if source_currency not in {"NGN", "USDT"}:
            raise QuoteError("only NGN and USDT are supported")
        # Infer target currency from source if not specified
        if target_currency is None:
            target_currency = "USDT" if source_currency == "NGN" else "NGN"
        if target_currency not in {"NGN", "USDT"}:
            raise QuoteError("only NGN and USDT are supported")
        if source_currency == target_currency:
            raise QuoteError("source and target currencies must differ")
        if isinstance(source_amount_minor, bool) or not isinstance(source_amount_minor, int) or source_amount_minor <= 0:
            raise QuoteError("source_amount_minor must be a positive integer")
        try:
            if rate is None:
                decimal_rate = self.ngn_per_usdt
            else:
                decimal_rate = Decimal(str(rate))
        except (InvalidOperation, ValueError):
            raise QuoteError("rate must be a valid positive decimal") from None
        if not decimal_rate.is_finite() or decimal_rate <= 0:
            raise QuoteError("rate must be a valid positive decimal")
        # rate means NGN per USDT. NGN minor units are kobo; USDT minor units are micro-USDT.
        fee_multiplier = Decimal(10_000 - self.fee_bps) / Decimal(10_000)
        if source_currency == "NGN":
            gross_target = Decimal(source_amount_minor) * Decimal(10_000) / decimal_rate
        else:
            gross_target = Decimal(source_amount_minor) * decimal_rate / Decimal(10_000)
        target_amount = int((gross_target * fee_multiplier).to_integral_value(rounding=ROUND_DOWN))
        if target_amount <= 0:
            raise QuoteError("quoted target amount rounds to zero")
        timestamp = time.time() if now is None else float(now)
        quote = {
            "quote_id": str(uuid.uuid4()), "account": account,
            "source_currency": source_currency, "target_currency": target_currency,
            "source_amount_minor": source_amount_minor, "target_amount_minor": target_amount,
            "rate": str(decimal_rate), "fee_bps": self.fee_bps,
            "expires_at": timestamp + self.ttl_seconds,
        }
        with self._lock:
            self.db.execute(
                "INSERT INTO conversion_quotes(quote_id, account, source_currency, target_currency, "
                "source_amount_minor, target_amount_minor, rate, fee_bps, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (quote["quote_id"], account, source_currency, target_currency, source_amount_minor,
                 target_amount, str(decimal_rate), self.fee_bps, quote["expires_at"])
            )
        return quote

    def execute(self, quote_id: str, idempotency_key: str, now: float | None = None) -> dict[str, Any]:
        if not quote_id or not idempotency_key:
            raise QuoteError("quote_id and idempotency_key are required")
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # Idempotent retries return the recorded operation even if quote is now consumed.
                prior = self.db.execute(
                    "SELECT result_json FROM operations WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if prior is not None:
                    prior_result = json.loads(prior["result_json"])
                    request_row = self.db.execute(
                        "SELECT request_json FROM operations WHERE idempotency_key = ?", (idempotency_key,)
                    ).fetchone()
                    request = json.loads(request_row["request_json"])
                    if request.get("quote_id") != quote_id:
                        from .wallet_ledger import IdempotencyConflict
                        raise IdempotencyConflict("idempotency key was used for another quote")
                    self.db.execute("COMMIT")
                    return prior_result
                row = self.db.execute("SELECT * FROM conversion_quotes WHERE quote_id = ?", (quote_id,)).fetchone()
                if row is None:
                    raise QuoteError("quote not found")
                if timestamp > float(row["expires_at"]):
                    raise QuoteExpired("quote has expired")
                if row["consumed_by"] is not None:
                    raise QuoteExpired("quote has already been consumed")
                request = {"quote_id": quote_id, "account": row["account"],
                           "source_currency": row["source_currency"], "target_currency": row["target_currency"],
                           "source_amount_minor": row["source_amount_minor"],
                           "target_amount_minor": row["target_amount_minor"]}
                available = self.ledger.balance(row["account"], row["source_currency"])
                if available < row["source_amount_minor"]:
                    raise InsufficientFunds(
                        f"insufficient {row['source_currency']}: have {available}, need {row['source_amount_minor']}"
                    )
                op_id = f"conversion-quote:{idempotency_key}"
                result = {"operation_id": op_id, **request, "status": "simulated"}
                self.ledger._record_operation(op_id, idempotency_key, "conversion_quote", request, result, [
                    (row["account"], row["source_currency"], -row["source_amount_minor"], "conversion quote debit"),
                    ("clearing", row["source_currency"], row["source_amount_minor"], "conversion source clearing"),
                    ("clearing", row["target_currency"], -row["target_amount_minor"], "conversion target clearing"),
                    (row["account"], row["target_currency"], row["target_amount_minor"], "conversion quote credit"),
                ])
                self.db.execute(
                    "UPDATE conversion_quotes SET consumed_by = ? WHERE quote_id = ? AND consumed_by IS NULL",
                    (idempotency_key, quote_id)
                )
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise

class LiquidityPositions:
    """Simulated pool-position lifecycle; valuations and fees are caller supplied."""

    def __init__(self, ledger: WalletLedger):
        self.ledger = ledger
        self.db = ledger.db
        self._lock = ledger._lock
        with self._lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS liquidity_positions (
                    position_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    pool_id TEXT NOT NULL,
                    principal_minor INTEGER NOT NULL CHECK(principal_minor > 0),
                    current_value_minor INTEGER NOT NULL CHECK(current_value_minor >= 0),
                    accrued_fees_minor INTEGER NOT NULL CHECK(accrued_fees_minor >= 0),
                    status TEXT NOT NULL CHECK(status IN ('open', 'active', 'closed')),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def open_position(self, account: str, pool_id: str, asset: str, target_currency: str,
                  amount_minor: int, idempotency_key: str) -> dict[str, Any]:
        if not account or not pool_id or not asset or not target_currency or not idempotency_key:
            raise ValueError("account, pool_id, asset, target_currency and idempotency_key are required")
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
            raise ValueError("amount_minor must be a positive integer")
        request = {"account": account, "pool_id": pool_id, "asset": asset, "target_currency": target_currency,
                   "amount_minor": amount_minor}
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.db.execute(
                    "SELECT request_json, result_json FROM operations WHERE idempotency_key = ?",
                    (idempotency_key,)
                ).fetchone()
                if existing:
                    if existing["request_json"] != json.dumps(request, sort_keys=True, separators=(",", ":")):
                        from .wallet_ledger import IdempotencyConflict
                        raise IdempotencyConflict("idempotency key was used for another request")
                    self.db.execute("COMMIT")
                    return json.loads(existing["result_json"])
                available = self.ledger.balance(account, "USDT")
                if available < amount_minor:
                    raise InsufficientFunds(f"insufficient USDT: have {available}, need {amount_minor}")
                position_id = str(uuid.uuid4())
                op_id = f"open-position:{idempotency_key}"
                result = {"operation_id": op_id, "position_id": position_id, "account": account,
                          "pool_id": pool_id, "principal_minor": amount_minor, "status": "active"}
                self.ledger._record_operation(op_id, idempotency_key, "open_position", request, result, [
                    (account, "USDT", -amount_minor, "simulated liquidity deposit"),
                    ("liquidity-clearing", "USDT", amount_minor, "simulated liquidity deposit clearing"),
                ])
                self.db.execute(
                    "INSERT INTO liquidity_positions(position_id, account, pool_id, principal_minor, "
                    "current_value_minor, accrued_fees_minor, status, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, 0, 'active', ?)",
                    (position_id, account, pool_id, amount_minor, amount_minor, idempotency_key)
                )
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def mark_to_market(self, position_id: str, current_value_minor: int,
                       accrued_fees_minor: int = 0) -> dict[str, Any]:
        if isinstance(current_value_minor, bool) or not isinstance(current_value_minor, int) or current_value_minor < 0:
            raise ValueError("current_value_minor must be a non-negative integer")
        if isinstance(accrued_fees_minor, bool) or not isinstance(accrued_fees_minor, int) or accrued_fees_minor < 0:
            raise ValueError("accrued_fees_minor must be a non-negative integer")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT * FROM liquidity_positions WHERE position_id = ?", (position_id,)).fetchone()
                if row is None:
                    raise ValueError("position not found")
                if row["status"] not in {"open", "active"}:
                    from .wallet_ledger import InvalidOperation
                    raise InvalidOperation("position is not open")
                self.db.execute(
                    "UPDATE liquidity_positions SET current_value_minor = ?, accrued_fees_minor = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE position_id = ?",
                    (current_value_minor, accrued_fees_minor, position_id)
                )
                self.db.execute("COMMIT")
                return self.get_position(position_id)
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def get_position(self, position_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM liquidity_positions WHERE position_id = ?", (position_id,)).fetchone()
            if row is None:
                raise ValueError("position not found")
            position = dict(row)
            position["unrealized_pnl_usdt_micro"] = position["current_value_minor"] + position["accrued_fees_minor"] - position["principal_minor"]
            return position

    def list_positions(self, account: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if account is None:
                rows = self.db.execute("SELECT * FROM liquidity_positions ORDER BY created_at, position_id").fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM liquidity_positions WHERE account = ? ORDER BY created_at, position_id", (account,)
                ).fetchall()
            return [dict(row) for row in rows]

    def close_position(self, position_id: str, idempotency_key: str) -> dict[str, Any]:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.db.execute(
                    "SELECT result_json FROM operations WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing:
                    self.db.execute("COMMIT")
                    return json.loads(existing["result_json"])
                row = self.db.execute("SELECT * FROM liquidity_positions WHERE position_id = ?", (position_id,)).fetchone()
                if row is None:
                    raise ValueError("position not found")
                if row["status"] not in {"open", "active"}:
                    from .wallet_ledger import InvalidOperation
                    raise InvalidOperation("position is not open")
                return_amount = int(row["current_value_minor"]) + int(row["accrued_fees_minor"])
                op_id = f"close-position:{idempotency_key}"
                request = {"position_id": position_id, "return_amount_minor": return_amount}
                result = {"operation_id": op_id, "position_id": position_id, "account": row["account"],
                          "returned_usdt_micro": return_amount, "return_amount_minor": return_amount, "status": "closed"}
                entries = []
                if return_amount:
                    entries = [
                        (row["account"], "USDT", return_amount, "simulated liquidity withdrawal"),
                        ("liquidity-clearing", "USDT", -return_amount, "simulated liquidity withdrawal clearing"),
                    ]
                self.ledger._record_operation(op_id, idempotency_key, "close_position", request, result, entries)
                self.db.execute(
                    "UPDATE liquidity_positions SET status = 'closed', updated_at = CURRENT_TIMESTAMP "
                    "WHERE position_id = ?", (position_id,)
                )
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise
