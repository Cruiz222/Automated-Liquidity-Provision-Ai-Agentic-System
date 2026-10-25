"""Account valuation and allocation deployment.

Valuation is conservative and explicit about its rate: NGN holdings are valued in USDT at the *ask* (what it
would cost to buy USDT), so the figure is what the customer could actually obtain. Every valuation reports
its rate source and time.
"""
from __future__ import annotations

import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from ..dex.selector import CapitalAllocation
from ..fx import ConversionQuotes, RateQuote, RateService
from ..ledger import InsufficientFunds, WalletLedger
from .positions import LiquidityPositions

MICRO = Decimal(1_000_000)


class PortfolioManager:
    def __init__(self, ledger: WalletLedger, quotes: ConversionQuotes, positions: LiquidityPositions,
                 rate_service: RateService | None = None) -> None:
        self.ledger, self.quotes, self.positions, self.rates = ledger, quotes, positions, rate_service

    def deploy(self, account: str, allocation: CapitalAllocation, idempotency_prefix: str) -> list[dict[str, Any]]:
        """Open one position per allocated pool. Checks total funds first; retries are idempotent per pool."""
        needed = allocation.allocated_minor
        available = self.ledger.balance(account, "USDT")
        if needed > available:
            # positions already opened by an earlier attempt are already debited; only block fresh shortfalls
            opened = sum(p["principal_minor"] for p in self.positions.list_positions(account)
                         if p["idempotency_key"].startswith(f"{idempotency_prefix}:"))
            if needed - opened > available:
                raise InsufficientFunds(f"allocation needs {needed} micro-USDT, available {available}")
        results = []
        for pool_id, amount in sorted(allocation.allocations.items()):
            token0, token1 = allocation.details.get(pool_id, {}).get("tokens", ["", ""])
            results.append(self.positions.open_position(
                account, pool_id, token0 or "?", token1 or "?", amount, f"{idempotency_prefix}:{pool_id}",
                entry_price=allocation.details.get(pool_id, {}).get("entry_price")))
        return results

    def _rate(self, rate: RateQuote | None) -> RateQuote:
        if rate is not None:
            return rate
        if self.rates is not None:
            return self.rates.current()
        fixed = self.quotes.ngn_per_usdt
        return RateQuote(fixed, fixed, time.time(), "static")

    def valuation(self, account: str, rate: RateQuote | None = None) -> dict[str, Any]:
        rate = self._rate(rate)
        balances = self.ledger.balances(account)
        pending = {"NGN": 0, "USDT": 0}
        for withdrawal in self.ledger.list_withdrawals(account):
            if withdrawal["status"] in ("pending", "processing"):
                pending[withdrawal["currency"]] += withdrawal["amount_minor"] + withdrawal["fee_minor"]

        def ngn_to_usdt(kobo: int) -> int:
            return int((Decimal(kobo) / Decimal(100) / rate.ask * MICRO).to_integral_value(rounding=ROUND_DOWN))

        positions = self.positions.list_positions(account)
        open_positions = [p for p in positions if p["status"] != "closed"]
        position_value = sum(p["current_value_minor"] + p["accrued_fees_minor"] for p in open_positions)
        principal = sum(p["principal_minor"] for p in open_positions)
        realized = sum(p["realized_pnl_minor"] or 0 for p in positions if p["status"] == "closed")
        total_usdt = (balances["USDT"] + pending["USDT"] + position_value
                      + ngn_to_usdt(balances["NGN"] + pending["NGN"]))
        return {
            "account": account,
            "balances": balances,
            "pending_withdrawals": pending,
            "open_positions": len(open_positions),
            "position_value_usdt_micro": position_value,
            "position_principal_usdt_micro": principal,
            "unrealized_pnl_usdt_micro": position_value - principal,
            "realized_pnl_usdt_micro": realized,
            "total_value_usdt_micro": total_usdt,
            "total_value_ngn_kobo": int((Decimal(total_usdt) / MICRO * rate.bid * 100).to_integral_value(rounding=ROUND_DOWN)),
            "rate": {"bid": str(rate.bid), "ask": str(rate.ask), "source": rate.source, "as_of": rate.as_of},
            "positions": [{
                "position_id": p["position_id"], "pool_id": p["pool_id"], "pair": f"{p['asset']}/{p['quote_asset']}",
                "status": p["status"], "principal_usdt_micro": p["principal_minor"],
                "value_usdt_micro": p["current_value_minor"], "fees_usdt_micro": p["accrued_fees_minor"],
                "pnl_usdt_micro": p["unrealized_pnl_usdt_micro"] if p["status"] != "closed" else p["realized_pnl_minor"],
            } for p in positions],
        }
