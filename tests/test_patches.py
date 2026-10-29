"""Regression tests for the wallet-ledger / service patches.

* fix #2: WalletLedger.request_withdrawal accepts limits=...
* fix #1: Service._mark uses sqrt(price/entry), not (1 + v2_il(r))
* fix #6: Service._mark cannot exceed max_mark_multiple
* fix #3: positions.open_position enforces pool capacity inside its transaction
"""
from __future__ import annotations

import dataclasses
import math
import unittest

from app.ledger import CurrencyLimits, InsufficientFunds, LimitExceeded, WalletLedger
from app.portfolio.positions import LiquidityPositions
from tests import test_service as ts


class RequestWithdrawalLimitsTests(unittest.TestCase):
    """fix #2 — the limits= kwarg must be accepted and honoured."""

    def setUp(self):
        self.ledger = WalletLedger(":memory:")
        self.ledger.deposit("alice", "NGN", 10_000_000, "dep-1")  # NGN 100,000

    def tearDown(self):
        self.ledger.close()

    def test_custom_limits_override_defaults(self):
        tight = CurrencyLimits(min_amount_minor=1, max_amount_minor=50_000,
                               daily_cap_minor=50_000, fee_minor=0)
        with self.assertRaises(LimitExceeded):
            self.ledger.request_withdrawal(
                "alice", "NGN", 60_000,
                {"bank_code": "058", "account_number": "0123456789"},
                "wd-1", limits=tight)

    def test_within_custom_limits_succeeds(self):
        tight = CurrencyLimits(min_amount_minor=1, max_amount_minor=50_000,
                               daily_cap_minor=50_000, fee_minor=0)
        row = self.ledger.request_withdrawal(
            "alice", "NGN", 40_000,
            {"bank_code": "058", "account_number": "0123456789"},
            "wd-2", limits=tight)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["amount_minor"], 40_000)
        self.assertEqual(row["fee_minor"], 0)

    def test_default_limits_still_work(self):
        # default NGN min is 10_000 kobo, so 5_000 must be rejected
        with self.assertRaises(LimitExceeded):
            self.ledger.request_withdrawal(
                "alice", "NGN", 5_000,
                {"bank_code": "058", "account_number": "0123456789"},
                "wd-3")


class PoolCapacityTests(unittest.TestCase):
    """fix #3 — capacity is enforced inside open_position's transaction."""

    def setUp(self):
        self.ledger = WalletLedger(":memory:")
        self.ledger.deposit("alice", "USDT", 10_000_000, "dep-1")
        self.ledger.deposit("bob",   "USDT", 10_000_000, "dep-2")
        self.positions = LiquidityPositions(self.ledger, clock=lambda: 1_000.0)

    def tearDown(self):
        self.ledger.close()

    def test_first_open_succeeds(self):
        row = self.positions.open_position(
            "alice", "pool-1", "ETH", "USDT", 3_000_000, "op-1",
            pool_capacity_minor=5_000_000)
        self.assertEqual(row["principal_minor"], 3_000_000)

    def test_second_open_over_capacity_fails(self):
        self.positions.open_position("alice", "pool-1", "ETH", "USDT", 3_000_000, "op-1",
                                     pool_capacity_minor=5_000_000)
        with self.assertRaises(LimitExceeded):
            self.positions.open_position("bob", "pool-1", "ETH", "USDT", 3_000_000, "op-2",
                                         pool_capacity_minor=5_000_000)

    def test_capacity_is_per_pool(self):
        self.positions.open_position("alice", "pool-1", "ETH", "USDT", 4_000_000, "op-1",
                                     pool_capacity_minor=5_000_000)
        # different pool: no conflict
        self.positions.open_position("bob", "pool-2", "BTC", "USDT", 4_000_000, "op-2",
                                     pool_capacity_minor=5_000_000)

    def test_no_capacity_means_unlimited(self):
        for i, account in enumerate(("alice", "bob", "alice", "bob")):
            self.positions.open_position(account, "pool-1", "ETH", "USDT", 1_000_000, f"op-{i}")
        self.assertEqual(self.positions.pool_exposure("pool-1"), 4_000_000)


class MarkValuationTests(ts.ServiceCase):
    """fix #1 and #6 - drive the real PlatformService._mark through list_positions."""

    def _value_ratio(self, factor, pool=ts.ETH_POOL, principal=200_000_000):
        service = self.make()
        user = self.user()
        self.fund(user)
        service.open_position(user, pool, principal, "open-pos-001")
        self.provider.overrides[pool] = lambda s: dataclasses.replace(
            s, price_history=s.price_history[:-1] + (s.price_history[-1] * factor,))
        self.clock.now += 40
        position = service.list_positions(user)[0]
        return position, principal

    def test_sqrt_valuation_at_double(self):
        position, principal = self._value_ratio(2.0)
        self.assertAlmostEqual((position["value_minor"] - position["fees_minor"]) / principal, math.sqrt(2), places=3)

    def test_sqrt_valuation_at_half(self):
        position, principal = self._value_ratio(0.5)
        self.assertAlmostEqual((position["value_minor"] - position["fees_minor"]) / principal, math.sqrt(0.5), places=3)

    def test_ceiling_clamps_absurd_price_moves(self):
        position, principal = self._value_ratio(100.0)      # sqrt(100) = 10x, far above the 2x ceiling
        self.assertLessEqual(position["value_minor"], 2 * principal)


if __name__ == "__main__":
    unittest.main()
