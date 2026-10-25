"""Ledger-backed simulated liquidity positions.

Opening a position debits the account's USDT and closing credits value plus fees, so positions can never
exist without funds. Valuations are supplied by the platform (never by the customer), bounded, monotonic in
fees and time, and every mark is stored with its source.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Callable

from ..ledger import IdempotencyConflict, InsufficientFunds, InvalidOperation, WalletLedger, ensure_columns


class PositionNotFound(LookupError):
    """No such position for this account (also used when it belongs to someone else)."""


class LiquidityPositions:
    """Simulated pool-position lifecycle with sanity-checked, audited valuations.

    ``mark_to_market`` rejects values above ``max_mark_multiple`` times principal
    (value plus fees), fees that decrease, and marks older than the previous one.
    Every accepted mark is kept in ``position_marks`` with its source.
    """

    def __init__(self, ledger: WalletLedger, max_mark_multiple: int | float = 2, clock: Callable[[], float] = time.time):
        if not isinstance(max_mark_multiple, (int, float)) or isinstance(max_mark_multiple, bool) or max_mark_multiple < 1:
            raise ValueError("max_mark_multiple must be at least 1")
        self.ledger = ledger
        self.db = ledger.db
        self.max_mark_multiple = max_mark_multiple
        self.clock = clock
        with ledger.transaction():
            self.db.execute("""
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
                )""")
            ensure_columns(self.db, "liquidity_positions", {
                "asset": "TEXT NOT NULL DEFAULT ''",
                "quote_asset": "TEXT NOT NULL DEFAULT ''",
                "closed_value_minor": "INTEGER",
                "realized_pnl_minor": "INTEGER",
                "entry_price": "REAL",
                "opened_ts": "REAL",
            })
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS position_marks (
                    mark_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL REFERENCES liquidity_positions(position_id),
                    value_minor INTEGER NOT NULL,
                    fees_minor INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    as_of REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")

    def open_position(self, account: str, pool_id: str, asset: str, target_currency: str,
                      amount_minor: int, idempotency_key: str, entry_price: float | None = None) -> dict[str, Any]:
        """Fund a position from the account's USDT. ``asset``/``target_currency`` name the pool's two tokens."""
        if not account or not pool_id or not asset or not target_currency or not idempotency_key:
            raise ValueError("account, pool_id, asset, target_currency and idempotency_key are required")
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
            raise ValueError("amount_minor must be a positive integer")
        request = {"account": account, "pool_id": pool_id, "asset": asset, "target_currency": target_currency,
                   "amount_minor": amount_minor}
        with self.ledger.transaction():
            existing = self.db.execute(
                "SELECT request_json, result_json FROM operations WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_json"] != json.dumps(request, sort_keys=True, separators=(",", ":")):
                    raise IdempotencyConflict("idempotency key was used for another request")
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
                "current_value_minor, accrued_fees_minor, status, idempotency_key, asset, quote_asset, "
                "entry_price, opened_ts) VALUES (?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?)",
                (position_id, account, pool_id, amount_minor, amount_minor, idempotency_key, asset,
                 target_currency, entry_price, self.clock()))
            return result

    def mark_to_market(self, position_id: str, current_value_minor: int, accrued_fees_minor: int = 0,
                       source: str = "manual", as_of: float | None = None) -> dict[str, Any]:
        for name, value in (("current_value_minor", current_value_minor), ("accrued_fees_minor", accrued_fees_minor)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not source or not isinstance(source, str):
            raise ValueError("source is required")
        stamp = self.clock() if as_of is None else float(as_of)
        with self.ledger.transaction():
            row = self.db.execute("SELECT * FROM liquidity_positions WHERE position_id = ?", (position_id,)).fetchone()
            if row is None:
                raise PositionNotFound("position not found")
            if row["status"] not in {"open", "active"}:
                raise InvalidOperation("position is not open")
            if current_value_minor + accrued_fees_minor > row["principal_minor"] * self.max_mark_multiple:
                raise InvalidOperation(
                    f"mark exceeds {self.max_mark_multiple}x principal; check the valuation source")
            if accrued_fees_minor < row["accrued_fees_minor"]:
                raise InvalidOperation("accrued fees cannot decrease")
            last = self.db.execute(
                "SELECT MAX(as_of) FROM position_marks WHERE position_id = ?", (position_id,)).fetchone()[0]
            if last is not None and stamp < last:
                raise InvalidOperation("mark is older than the previous mark")
            self.db.execute(
                "INSERT INTO position_marks(position_id, value_minor, fees_minor, source, as_of) "
                "VALUES (?, ?, ?, ?, ?)", (position_id, current_value_minor, accrued_fees_minor, source, stamp))
            self.db.execute(
                "UPDATE liquidity_positions SET current_value_minor = ?, accrued_fees_minor = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE position_id = ?",
                (current_value_minor, accrued_fees_minor, position_id))
        return self.get_position(position_id)

    @staticmethod
    def _present(row: sqlite3.Row) -> dict[str, Any]:
        position = dict(row)
        if position["status"] == "closed":
            position["unrealized_pnl_usdt_micro"] = 0
        else:
            position["unrealized_pnl_usdt_micro"] = (
                position["current_value_minor"] + position["accrued_fees_minor"] - position["principal_minor"])
        return position

    def get_position(self, position_id: str, *, account: str | None = None) -> dict[str, Any]:
        """Fetch a position. With ``account`` another account's position is reported as not found."""
        with self.ledger.lock:
            row = self.db.execute("SELECT * FROM liquidity_positions WHERE position_id = ?", (position_id,)).fetchone()
            if row is None or (account is not None and row["account"] != account):
                raise PositionNotFound("position not found")
            return self._present(row)

    def list_positions(self, account: str | None = None) -> list[dict[str, Any]]:
        with self.ledger.lock:
            if account is None:
                rows = self.db.execute("SELECT * FROM liquidity_positions ORDER BY created_at, position_id").fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM liquidity_positions WHERE account = ? ORDER BY created_at, position_id", (account,)
                ).fetchall()
            return [self._present(row) for row in rows]

    def pool_exposure(self, pool_id: str) -> int:
        """Total principal currently placed in a pool across all accounts."""
        with self.ledger.lock:
            return int(self.db.execute(
                "SELECT COALESCE(SUM(principal_minor), 0) FROM liquidity_positions WHERE pool_id = ? AND status != 'closed'",
                (pool_id,)).fetchone()[0])

    def count_open(self, account: str) -> int:
        with self.ledger.lock:
            return int(self.db.execute(
                "SELECT COUNT(*) FROM liquidity_positions WHERE account = ? AND status != 'closed'",
                (account,)).fetchone()[0])

    def marks(self, position_id: str) -> list[dict[str, Any]]:
        with self.ledger.lock:
            rows = self.db.execute(
                "SELECT * FROM position_marks WHERE position_id = ? ORDER BY mark_id", (position_id,)).fetchall()
            return [dict(row) for row in rows]

    def close_position(self, position_id: str, idempotency_key: str, *, account: str) -> dict[str, Any]:
        """Close a position owned by ``account`` (another account's position is reported as not found)."""
        if not idempotency_key or not account:
            raise ValueError("idempotency_key and account are required")
        with self.ledger.transaction():
            existing = self.db.execute(
                "SELECT request_json, result_json FROM operations WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                recorded = json.loads(existing["request_json"])
                if recorded.get("position_id") != position_id or json.loads(existing["result_json"]).get("account") != account:
                    raise IdempotencyConflict("idempotency key was used for another position")
                return json.loads(existing["result_json"])
            row = self.db.execute("SELECT * FROM liquidity_positions WHERE position_id = ?", (position_id,)).fetchone()
            if row is None or row["account"] != account:
                raise PositionNotFound("position not found")
            if row["status"] not in {"open", "active"}:
                raise InvalidOperation("position is not open")
            return_amount = int(row["current_value_minor"]) + int(row["accrued_fees_minor"])
            realized = return_amount - int(row["principal_minor"])
            op_id = f"close-position:{idempotency_key}"
            request = {"position_id": position_id, "return_amount_minor": return_amount}
            result = {"operation_id": op_id, "position_id": position_id, "account": row["account"],
                      "returned_usdt_micro": return_amount, "return_amount_minor": return_amount,
                      "realized_pnl_minor": realized, "status": "closed"}
            entries = []
            if return_amount:
                entries = [
                    (row["account"], "USDT", return_amount, "simulated liquidity withdrawal"),
                    ("liquidity-clearing", "USDT", -return_amount, "simulated liquidity withdrawal clearing"),
                ]
            self.ledger._record_operation(op_id, idempotency_key, "close_position", request, result, entries)
            self.db.execute(
                "UPDATE liquidity_positions SET status = 'closed', closed_value_minor = ?, realized_pnl_minor = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE position_id = ?", (return_amount, realized, position_id))
            return result
