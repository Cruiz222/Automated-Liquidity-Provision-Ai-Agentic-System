"""Command-line walkthrough on the real service layer (in memory, simulated):

    python3 -m app.demo

Registers a user, makes a simulated deposit, converts NGN to USDT, asks for an allocation, opens positions,
withdraws, and reconciles the ledger. The example pool data is synthetic.
"""
from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .service import PlatformService

DEMO_PASSWORD = "demo-password-123"
DEMO_PIN = "482915"


def run(settings: Settings | None = None) -> dict[str, Any]:
    service = PlatformService(settings or Settings(database=":memory:", password_iterations=1000))
    try:
        user = service.accounts.register("demo-user", DEMO_PASSWORD)["user_id"]
        service.accounts.set_pin(user, DEMO_PASSWORD, DEMO_PIN)                           # a PIN comes before any transaction
        service.deposit(user, "NGN", 500_000_000, "demo-deposit-1", pin=DEMO_PIN)                       # NGN 5,000,000
        quote = service.create_quote(user, "NGN", 450_000_000)                            # convert NGN 4,500,000
        conversion = service.execute_quote(user, quote["quote_id"], "demo-convert-1", pin=DEMO_PIN)
        usdt = service.wallet(user)["balances"]["USDT"]
        recommendation = service.recommendation(usdt)
        positions = service.deploy_recommendation(user, usdt, "demo-deploy-1", pin=DEMO_PIN)
        service.kyc_basic(user, "Demo User", "12345678901")                              # simulated identity check
        withdrawal = service.request_withdrawal(
            user, "NGN", 10_000_000, {"bank_code": "058", "account_number": "0123456785"}, "demo-withdraw-1", pin=DEMO_PIN)
        service.simulate_payout(user, withdrawal["withdrawal_id"], "complete")
        return {
            "quote": {k: quote[k] for k in ("source_amount_minor", "target_amount_minor", "fee_minor", "rate", "rate_source")},
            "conversion": conversion["status"],
            "rationale": recommendation["rationale"],
            "positions_opened": len(positions),
            "wallet": service.wallet(user),
            "withdrawal": service.ledger.get_withdrawal(withdrawal["withdrawal_id"])["status"],
            "fees_booked": {c: service.ledger.balance("fees", c) for c in ("NGN", "USDT")},
            "reconciliation": service.ledger.reconcile(),
        }
    finally:
        service.close()


def main() -> None:
    report = run()
    print(report.pop("rationale"))
    print()
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
