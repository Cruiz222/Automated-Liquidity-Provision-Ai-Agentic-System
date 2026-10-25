"""NGN <-> USDT conversion quotes: short-lived, single-use, owned by one account.

Rates come from a :class:`~app.fx.pricing.RateService` (or a fixed rate for tests). Every quote records its
rate source and time, and the platform fee is booked to the ``fees`` ledger account. Nothing here talks to a
bank, exchange or chain.
"""
from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal, InvalidOperation as DecimalInvalid, ROUND_DOWN
from typing import Any

from ..ledger import IdempotencyConflict, InsufficientFunds, InvalidOperation, WalletLedger, ensure_columns
from .pricing import RateError, RateService


class QuoteError(Exception):
    pass


class QuoteExpired(QuoteError):
    pass


class QuoteAlreadyUsed(QuoteError):
    pass


def _positive_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise InvalidOperation(f"{field} must be a valid decimal")
    try:
        result = Decimal(str(value))
    except (DecimalInvalid, ValueError):
        raise InvalidOperation(f"{field} must be a valid decimal") from None
    if not result.is_finite() or result <= 0:
        raise InvalidOperation(f"{field} must be positive")
    return result


class ConversionQuotes:
    """Creates short-lived conversion quotes and executes each at most once.

    With a ``rate_service`` the customer buys USDT at the ask and sells it at the
    bid, and each quote records the rate source and timestamp. Without one, the
    fixed ``ngn_per_usdt`` is used for both sides. ``fee_bps`` is a platform fee
    on the target amount; it is booked to the ``fees`` account.
    """

    def __init__(self, ledger: WalletLedger, ngn_per_usdt: Decimal | str | int | float = Decimal("1500"),
                 fee_bps: int = 50, ttl_seconds: int = 30, rate_service: RateService | None = None,
                 min_amount_minor: dict[str, int] | None = None, max_amount_minor: dict[str, int] | None = None,
                 max_open_quotes: int = 20):
        self.ngn_per_usdt = _positive_decimal(ngn_per_usdt, "ngn_per_usdt")
        if isinstance(fee_bps, bool) or not isinstance(fee_bps, int) or not 0 <= fee_bps < 10_000:
            raise ValueError("fee_bps must be an integer from 0 to 9999")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        self.ledger = ledger
        self.fee_bps = fee_bps
        self.ttl_seconds = ttl_seconds
        self.rate_service = rate_service
        self.min_amount_minor = {"NGN": 10_000, "USDT": 1_000_000, **(min_amount_minor or {})}
        self.max_amount_minor = {"NGN": 5_000_000_000, "USDT": 50_000_000_000, **(max_amount_minor or {})}
        self.max_open_quotes = max_open_quotes
        self.db = ledger.db
        with ledger.transaction():
            self.db.execute("""
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
                )""")
            ensure_columns(self.db, "conversion_quotes", {
                "fee_minor": "INTEGER NOT NULL DEFAULT 0",
                "rate_source": "TEXT NOT NULL DEFAULT 'static'",
                "rate_as_of": "REAL",
            })

    def create_quote(self, account: str, source_currency: str, source_amount_minor: int,
                     target_currency: str | None = None,
                     rate: Decimal | str | int | float | None = None,
                     now: float | None = None) -> dict[str, Any]:
        if not account:
            raise QuoteError("account is required")
        if source_currency not in {"NGN", "USDT"}:
            raise QuoteError("only NGN and USDT are supported")
        if target_currency is None:
            target_currency = "USDT" if source_currency == "NGN" else "NGN"
        if target_currency not in {"NGN", "USDT"}:
            raise QuoteError("only NGN and USDT are supported")
        if source_currency == target_currency:
            raise QuoteError("source and target currencies must differ")
        if isinstance(source_amount_minor, bool) or not isinstance(source_amount_minor, int) or source_amount_minor <= 0:
            raise QuoteError("source_amount_minor must be a positive integer")
        if source_amount_minor < self.min_amount_minor[source_currency]:
            raise QuoteError(f"minimum conversion is {self.min_amount_minor[source_currency]} {source_currency} minor units")
        if source_amount_minor > self.max_amount_minor[source_currency]:
            raise QuoteError(f"maximum conversion is {self.max_amount_minor[source_currency]} {source_currency} minor units")
        timestamp = time.time() if now is None else float(now)
        with self.ledger.lock:
            open_quotes = self.db.execute(
                "SELECT COUNT(*) FROM conversion_quotes WHERE account = ? AND consumed_by IS NULL AND expires_at > ?",
                (account, timestamp)).fetchone()[0]
        if open_quotes >= self.max_open_quotes:
            raise QuoteError("too many open quotes; wait for them to expire or use one")
        rate_source, rate_as_of = "static", None
        if rate is not None:
            try:
                decimal_rate = Decimal(str(rate))
            except (DecimalInvalid, ValueError):
                raise QuoteError("rate must be a valid positive decimal") from None
            rate_source = "override"
        elif self.rate_service is not None:
            try:
                market = self.rate_service.current()
            except RateError as error:
                raise QuoteError(f"no trustworthy rate available: {error}") from error
            decimal_rate = market.ask if source_currency == "NGN" else market.bid
            rate_source, rate_as_of = market.source, market.as_of
        else:
            decimal_rate = self.ngn_per_usdt
        if not decimal_rate.is_finite() or decimal_rate <= 0:
            raise QuoteError("rate must be a valid positive decimal")
        # rate = NGN per USDT; NGN is in kobo (1/100 NGN), USDT in micro-USDT (1/1e6).
        if source_currency == "NGN":
            gross = Decimal(source_amount_minor) * Decimal(10_000) / decimal_rate
        else:
            gross = Decimal(source_amount_minor) * decimal_rate / Decimal(10_000)
        gross_minor = int(gross.to_integral_value(rounding=ROUND_DOWN))
        target_amount = int((gross * Decimal(10_000 - self.fee_bps) / Decimal(10_000)).to_integral_value(rounding=ROUND_DOWN))
        if target_amount <= 0:
            raise QuoteError("quoted target amount rounds to zero")
        fee_minor = gross_minor - target_amount
        quote = {
            "quote_id": str(uuid.uuid4()), "account": account,
            "source_currency": source_currency, "target_currency": target_currency,
            "source_amount_minor": source_amount_minor, "target_amount_minor": target_amount,
            "fee_minor": fee_minor, "rate": str(decimal_rate), "fee_bps": self.fee_bps,
            "rate_source": rate_source, "rate_as_of": rate_as_of,
            "expires_at": timestamp + self.ttl_seconds,
        }
        with self.ledger.transaction():
            self.db.execute("DELETE FROM conversion_quotes WHERE consumed_by IS NULL AND expires_at < ?",
                            (timestamp - 86_400,))
            self.db.execute(
                "INSERT INTO conversion_quotes(quote_id, account, source_currency, target_currency, "
                "source_amount_minor, target_amount_minor, rate, fee_bps, expires_at, fee_minor, "
                "rate_source, rate_as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (quote["quote_id"], account, source_currency, target_currency, source_amount_minor,
                 target_amount, str(decimal_rate), self.fee_bps, quote["expires_at"], fee_minor,
                 rate_source, rate_as_of))
        return quote

    def execute(self, quote_id: str, idempotency_key: str, *, account: str, now: float | None = None) -> dict[str, Any]:
        """Execute a quote for ``account``. A quote owned by another account is reported as not found."""
        if not quote_id or not idempotency_key or not account:
            raise QuoteError("quote_id, idempotency_key and account are required")
        timestamp = time.time() if now is None else float(now)
        with self.ledger.transaction():
            prior = self.db.execute(
                "SELECT request_json, result_json FROM operations WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if prior is not None:  # idempotent retry returns the recorded operation
                recorded = json.loads(prior["request_json"])
                if recorded.get("quote_id") != quote_id or recorded.get("account") != account:
                    raise IdempotencyConflict("idempotency key was used for another quote")
                return json.loads(prior["result_json"])
            row = self.db.execute("SELECT * FROM conversion_quotes WHERE quote_id = ?", (quote_id,)).fetchone()
            if row is None or row["account"] != account:
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
                    f"insufficient {row['source_currency']}: have {available}, need {row['source_amount_minor']}")
            op_id = f"conversion-quote:{idempotency_key}"
            result = {"operation_id": op_id, **request, "fee_minor": row["fee_minor"], "status": "simulated"}
            gross = row["target_amount_minor"] + row["fee_minor"]
            entries = [
                (row["account"], row["source_currency"], -row["source_amount_minor"], "conversion quote debit"),
                ("clearing", row["source_currency"], row["source_amount_minor"], "conversion source clearing"),
                ("clearing", row["target_currency"], -gross, "conversion target clearing"),
                (row["account"], row["target_currency"], row["target_amount_minor"], "conversion quote credit"),
            ]
            if row["fee_minor"]:
                entries.append(("fees", row["target_currency"], row["fee_minor"], "conversion fee"))
            self.ledger._record_operation(op_id, idempotency_key, "conversion_quote", request, result, entries)
            self.db.execute("UPDATE conversion_quotes SET consumed_by = ? WHERE quote_id = ? AND consumed_by IS NULL",
                            (idempotency_key, quote_id))
            return result


