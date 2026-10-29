import dataclasses
import unittest
from decimal import Decimal

from app.config import Settings
from app.dex import PoolSnapshot, load_snapshots
from app.fx import QuoteError
from app.ledger import InsufficientFunds, InvalidOperation, InvalidTransition, LimitExceeded
from app.portfolio import PositionNotFound
from app.service import NotFound, PlatformService, SimulationDisabled
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "pools_snapshot.json"
BANK = {"bank_code": "058", "account_number": "0123456785"}
USDC_POOL = "usdc-usdt-arbitrum-1bp"
ETH_POOL = "weth-usdc-arbitrum-5bp"


class Clock:
    def __init__(self, now=2_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class MutableProvider:
    def __init__(self, clock):
        self.clock, self.overrides = clock, {}

    def fetch_pools(self):
        out = []
        for s in load_snapshots(EXAMPLE)[0]:
            s = dataclasses.replace(s, as_of=self.clock.now, source="test")
            out.append(self.overrides.get(s.pool_id, lambda x: x)(s))
        return out


class ServiceCase(unittest.TestCase):
    def make(self, **settings):
        self.clock = Clock()
        self.provider = MutableProvider(self.clock)
        defaults = dict(database=":memory:", password_iterations=1000, pool_refresh_seconds=0, require_pin=False)
        self.service = PlatformService(Settings(**{**defaults, **settings}), clock=self.clock, pool_provider=self.provider)
        self.addCleanup(self.service.close)
        return self.service

    def user(self, name="alice"):
        return self.service.accounts.register(name, "correct-horse-battery", ip=name)["user_id"]

    _bvn = iter(range(10 ** 10, 10 ** 10 + 1000))

    def verify(self, user):
        self.service.kyc_basic(user, "Test Customer", str(next(self._bvn)))

    def fund(self, user, usdt_micro=3_000_000_000):
        self.service.deposit(user, "USDT", usdt_micro, f"fund-{user}-{usdt_micro}")


class TestPinEnforcement(ServiceCase):
    PIN = "482915"

    def setUp(self):
        self.make(require_pin=True)
        self.u = self.user()

    def pinned(self):
        self.service.accounts.set_pin(self.u, "correct-horse-battery", self.PIN)

    def test_nothing_moves_before_a_pin_exists(self):
        from app.accounts import PinNotSet
        with self.assertRaises(PinNotSet):
            self.service.deposit(self.u, "USDT", 1_000_000, "dep-key-0001", pin=self.PIN)
        self.assertEqual(self.service.wallet(self.u)["balances"]["USDT"], 0)

    def test_every_money_action_checks_the_pin(self):
        from app.accounts import InvalidPin, PinRequired
        self.pinned()
        self.service.deposit(self.u, "USDT", 2_000_000_000, "dep-key-0001", pin=self.PIN)
        self.service.deposit(self.u, "NGN", 100_000_000, "dep-key-0002", pin=self.PIN)
        quote = self.service.create_quote(self.u, "NGN", 15_000_000)
        pos = self.service.open_position(self.u, USDC_POOL, 500_000_000, "open-pos-001", pin=self.PIN)
        calls = {
            "deposit": lambda pin, k: self.service.deposit(self.u, "USDT", 1_000_000, k, pin=pin),
            "convert": lambda pin, k: self.service.execute_quote(self.u, quote["quote_id"], k, pin=pin),
            "open": lambda pin, k: self.service.open_position(self.u, USDC_POOL, 500_000_000, k, pin=pin),
            "deploy": lambda pin, k: self.service.deploy_recommendation(self.u, 600_000_000, k, pin=pin),
            "close": lambda pin, k: self.service.close_position(self.u, pos["position_id"], k, pin=pin),
            "withdraw": lambda pin, k: self.service.request_withdrawal(self.u, "NGN", 4_000_000, {"account_number": "0123456785"}, k, pin=pin),
            "payout add": lambda pin, k: self.service.add_payout_account(self.u, "NGN", "Mine", {"account_number": "0123456785"}, pin=pin),
        }
        for name, call in calls.items():
            with self.assertRaises(PinRequired, msg=name):
                call(None, f"nopin-key-{name.replace(' ', '')}")
            with self.assertRaises(InvalidPin, msg=name):
                call("000000", f"badpin-key-{name.replace(' ', '')}")
            self.service.accounts.user_limiter.reset(f"pin:{self.u}")              # the lockout is tested separately
        self.assertEqual(self.service.wallet(self.u)["balances"]["USDT"], 2_000_000_000 - 500_000_000)    # nothing leaked through
        self.assertEqual(len(self.service.positions.list_positions(self.u)), 1)

    def test_replays_do_not_need_the_pin_again(self):
        self.pinned()
        first = self.service.deposit(self.u, "USDT", 1_000_000, "dep-key-0001", pin=self.PIN)
        self.assertEqual(self.service.deposit(self.u, "USDT", 1_000_000, "dep-key-0001"), first)
        self.service.deposit(self.u, "USDT", 2_000_000_000, "dep-key-0002", pin=self.PIN)
        opened = self.service.deploy_recommendation(self.u, 2_000_000_000, "deploy-key-01", pin=self.PIN)
        again = self.service.deploy_recommendation(self.u, 2_000_000_000, "deploy-key-01")
        self.assertEqual([p["position_id"] for p in opened], [p["position_id"] for p in again])

    def test_cancelling_a_withdrawal_and_reading_data_need_no_pin(self):
        self.pinned()
        self.service.deposit(self.u, "NGN", 100_000_000, "dep-key-0001", pin=self.PIN)
        w = self.service.request_withdrawal(self.u, "NGN", 4_000_000, {"account_number": "0123456785"}, "wd-key-0001", pin=self.PIN)
        self.service.cancel_withdrawal(self.u, w["withdrawal_id"])
        self.service.wallet(self.u), self.service.pools(), self.service.list_positions(self.u)


class TestMailerSelection(unittest.TestCase):
    def test_console_by_default_and_smtp_when_configured(self):
        from app.accounts import ConsoleMailer, SmtpMailer
        plain = PlatformService(Settings(database=":memory:", password_iterations=1000))
        self.addCleanup(plain.close)
        self.assertIsInstance(plain.accounts.mailer, ConsoleMailer)
        smtp = PlatformService(Settings(database=":memory:", password_iterations=1000, smtp_host="smtp.example.com", smtp_from="no-reply@example.com"))
        self.addCleanup(smtp.close)
        self.assertIsInstance(smtp.accounts.mailer, SmtpMailer)
        with self.assertRaises(ValueError):
            Settings(smtp_host="smtp.example.com")                       # a sender address is required


class TestWalletAndValuation(ServiceCase):
    def test_valuation_regressions(self):
        service = self.make()
        u = self.user()
        service.deposit(u, "NGN", 15_000_000, "ngn-dep-001")
        self.assertEqual(service.wallet(u)["balances"]["NGN"], 15_000_000)
        # NGN 150,000 valued at the 1,515 ask is ~99 USDT (an earlier build showed 10,000 USDT).
        self.assertTrue(98_000_000 < service.wallet(u)["total_value_usdt_micro"] < 100_000_000)
        self.fund(u, 1_000_000_000)
        pos = service.open_position(u, USDC_POOL, 500_000_000, "open-pos-001")
        self.assertEqual(pos["status"], "active")
        # 1,000 USDT (half in a position) + ~99 USDT of NGN; an earlier build showed ~333,000 here.
        self.assertTrue(1_098_000_000 < service.wallet(u)["total_value_usdt_micro"] < 1_100_000_000)

    def test_simulated_deposit_caps_and_replay(self):
        service = self.make()
        u = self.user()
        with self.assertRaises(LimitExceeded):
            service.deposit(u, "USDT", 5_000_000_001, "big-dep-001")
        for i in range(4):
            service.deposit(u, "USDT", 5_000_000_000, f"day-dep-00{i}")
        with self.assertRaises(LimitExceeded):
            service.deposit(u, "USDT", 1_000_000, "day-dep-009")          # 20,000 USDT/day reached
        service.deposit(u, "USDT", 5_000_000_000, "day-dep-000")         # replay of an old key credits nothing new
        self.assertEqual(service.wallet(u)["balances"]["USDT"], 20_000_000_000)

    def test_input_validation(self):
        service = self.make()
        u = self.user()
        for amount in (0, -1, 1.5, True, "100", None, 10 ** 14):
            with self.assertRaises(InvalidOperation, msg=str(amount)):
                service.deposit(u, "USDT", amount, "valid-key-001")
        for currency in ("BTC", "usdt", None, 5):
            with self.assertRaises(InvalidOperation):
                service.deposit(u, currency, 100, "valid-key-002")
        for key in ("short", "has space in it", "x" * 101, "", None, "semi;colon-key"):
            with self.assertRaises(InvalidOperation, msg=str(key)):
                service.deposit(u, "USDT", 100, key)

    def test_simulation_can_be_disabled(self):
        service = self.make(simulation_mode=False)
        u = self.user()
        with self.assertRaises(SimulationDisabled):
            service.deposit(u, "USDT", 100, "valid-key-001")

    def test_history_is_scoped_and_paginated(self):
        service = self.make()
        a, b = self.user("alice"), self.user("bobby")
        for i in range(5):
            service.deposit(a, "USDT", 1_000_000, f"hist-dep-00{i}")
        service.deposit(b, "USDT", 7_000_000, "bobby-dep-001")
        page = service.history(a, 3)
        self.assertEqual(len(page), 3)
        older = service.history(a, 3, before=page[-1]["entry_id"])
        self.assertEqual(len(older), 2)
        self.assertTrue(all(e["amount_minor"] == 1_000_000 for e in page + older))


class TestWithdrawalsAndQuotes(ServiceCase):
    def test_withdrawal_lifecycle_and_ownership(self):
        service = self.make()
        a, b = self.user("alice"), self.user("bobby")
        self.verify(a)
        service.deposit(a, "NGN", 100_000_000, "ngn-dep-001")
        w = service.request_withdrawal(a, "NGN", 10_000_000, BANK, "wd-key-0001")
        wid = w["withdrawal_id"]
        for action in (lambda: service.cancel_withdrawal(b, wid), lambda: service.simulate_payout(b, wid, "complete")):
            with self.assertRaises(NotFound):
                action()
        with self.assertRaises(NotFound):
            service.cancel_withdrawal(a, "wd:not-mine")
        self.assertEqual(service.withdrawals(b), [])
        self.assertEqual(service.simulate_payout(a, wid, "complete")["status"], "completed")
        with self.assertRaises(InvalidTransition):
            service.simulate_payout(a, wid, "fail")
        with self.assertRaises(InvalidOperation):
            service.simulate_payout(a, wid, "explode")
        self.assertTrue(service.ledger.reconcile()["ok"])

    def test_cancel_returns_funds(self):
        service = self.make()
        a = self.user()
        self.verify(a)
        service.deposit(a, "NGN", 100_000_000, "ngn-dep-001")
        w = service.request_withdrawal(a, "NGN", 10_000_000, BANK, "wd-key-0001")
        service.cancel_withdrawal(a, w["withdrawal_id"])
        self.assertEqual(service.wallet(a)["balances"]["NGN"], 100_000_000)

    def test_quote_lifecycle_expiry_and_ownership(self):
        service = self.make()
        a, b = self.user("alice"), self.user("bobby")
        service.deposit(a, "NGN", 100_000_000, "ngn-dep-001")
        quote = service.create_quote(a, "NGN", 15_000_000)
        self.assertEqual(quote["rate_source"], "simulation-static")
        self.assertGreater(quote["fee_minor"], 0)
        with self.assertRaises(QuoteError):
            service.execute_quote(b, quote["quote_id"], "steal-key-001")
        result = service.execute_quote(a, quote["quote_id"], "conv-key-001")
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(service.execute_quote(a, quote["quote_id"], "conv-key-001"), result)
        self.assertEqual(service.ledger.balance("fees", "USDT"), quote["fee_minor"])
        late = service.create_quote(a, "NGN", 15_000_000)
        self.clock.now += 31
        with self.assertRaises(QuoteError):
            service.execute_quote(a, late["quote_id"], "conv-key-002")
        with self.assertRaises(NotFound):
            service.execute_quote(a, 12345, "conv-key-003")

    def test_stale_rate_blocks_quotes(self):
        service = self.make()
        a = self.user()
        service.rates.sources[0]._clock = lambda: 1.0      # the feed stops updating
        with self.assertRaises(QuoteError):
            service.create_quote(a, "NGN", 15_000_000)


class TestPools(ServiceCase):
    def test_example_pool_screening(self):
        service = self.make()
        by_id = {p["pool_id"]: p for p in service.pools()}
        self.assertTrue(by_id[USDC_POOL]["eligible"])
        self.assertFalse(by_id["newfi-usdc-base-30bp"]["eligible"])
        self.assertFalse(by_id["pepe-weth-ethereum-30bp"]["eligible"])
        self.assertEqual(by_id[USDC_POOL]["source"], "test")

    def test_stale_provider_data_excludes_everything(self):
        service = self.make()
        service.pools()
        self.provider.fetch_pools = lambda: []             # provider outage: only old snapshots remain
        self.clock.now += 3 * 86_400
        self.assertTrue(all(not p["eligible"] and any("stale" in r for r in p["reasons"]) for p in service.pools()))
        u = self.user()
        self.fund(u)
        with self.assertRaises(InvalidOperation):
            service.open_position(u, USDC_POOL, 500_000_000, "open-pos-001")

    def test_recommendation_report_is_auditable_and_does_not_move_funds(self):
        service = self.make()
        user = self.user()
        self.fund(user)

        balances_before = service.ledger.balances(user)
        positions_before = service.positions.list_positions(user)

        with service.ledger.lock:
            entries_before = service.ledger.db.execute(
                "SELECT COUNT(*) FROM ledger_entries"
            ).fetchone()[0]

        report = service.recommendation(3_000_000_000)

        balances_after = service.ledger.balances(user)
        positions_after = service.positions.list_positions(user)

        with service.ledger.lock:
            entries_after = service.ledger.db.execute(
                "SELECT COUNT(*) FROM ledger_entries"
            ).fetchone()[0]

        self.assertEqual(balances_after, balances_before)
        self.assertEqual(positions_after, positions_before)
        self.assertEqual(entries_after, entries_before)

        self.assertEqual(report["report_type"], "dex_pool_screening")
        self.assertEqual(report["total_capital_minor"], 3_000_000_000)
        self.assertEqual(
            report["allocated_minor"]
            + report["reserve_minor"]
            + report["unallocated_minor"],
            report["total_capital_minor"],
        )

        self.assertFalse(report["execution"]["performed"])
        self.assertFalse(report["execution"]["funds_moved"])
        self.assertFalse(report["execution"]["positions_changed"])

        self.assertTrue(report["pool_evaluations"])
        self.assertTrue(
            any(not pool["eligible"] for pool in report["pool_evaluations"])
        )

        selected_ids = {
            item["pool_id"] for item in report["allocations"]
        }
        for item in report["allocations"]:
            self.assertIn("fee_apr", item)
            self.assertIn("il_apr", item)
            self.assertIn("net_apr_after_gas", item)
            self.assertIn("source", item)

        for pool in report["pool_evaluations"]:
            if pool["pool_id"] in selected_ids:
                self.assertGreater(pool["selected_amount_minor"], 0)
            else:
                self.assertEqual(pool["selected_amount_minor"], 0)

    def test_recommendation_and_scenario(self):
        service = self.make()
        rec = service.recommendation(3_000_000_000)
        self.assertGreaterEqual(len(rec["allocations"]), 3)
        self.assertEqual(sum(a["amount_minor"] for a in rec["allocations"]) + rec["reserve_minor"] + rec["unallocated_minor"], 3_000_000_000)
        scenario = service.scenario(ETH_POOL, -50.0)
        self.assertAlmostEqual(scenario["impermanent_loss_pct"], -5.719, places=2)
        with self.assertRaises(NotFound):
            service.scenario("nope", -10.0)
        with self.assertRaises(InvalidOperation):
            service.scenario(ETH_POOL, -99.0)


class TestPositions(ServiceCase):
    def test_open_requires_funds_and_valid_pool(self):
        service = self.make()
        u = self.user()
        with self.assertRaises(InsufficientFunds):
            service.open_position(u, USDC_POOL, 500_000_000, "open-pos-001")      # the old build allowed this
        self.fund(u)
        with self.assertRaises(NotFound):
            service.open_position(u, "made-up-pool", 500_000_000, "open-pos-002")
        with self.assertRaises(InvalidOperation):
            service.open_position(u, "newfi-usdc-base-30bp", 500_000_000, "open-pos-003")
        with self.assertRaises(InvalidOperation):
            service.open_position(u, USDC_POOL, 1_000_000, "open-pos-004")          # below the minimum
        with self.assertRaises(InvalidOperation):
            service.open_position(u, USDC_POOL, 100_000_000, "open-pos-005")        # gas would eat the yield
        self.assertEqual(service.positions.list_positions(u), [])
        self.assertEqual(service.wallet(u)["balances"]["USDT"], 3_000_000_000)

    def test_pool_capacity_is_shared_across_users(self):
        service = self.make()
        a, b = self.user("alice"), self.user("bobby")
        cap = int(service.selector_config.max_pool_tvl_share * 60e6 * 1_000_000)      # 1% of a $60M pool
        service.ledger.deposit(a, "USDT", cap * 2, "seed-funds-a")                   # bypasses the simulated-deposit caps
        self.fund(b, 5_000_000_000)
        service.positions.open_position(a, USDC_POOL, "USDC", "USDT", cap - 100_000_000, "seed-pos-001")
        with self.assertRaises(LimitExceeded):
            service.open_position(b, USDC_POOL, 500_000_000, "open-pos-001")

    def test_position_limit(self):
        service = self.make(max_open_positions=2)
        u = self.user()
        self.fund(u)
        service.open_position(u, USDC_POOL, 500_000_000, "open-pos-001")
        service.open_position(u, USDC_POOL, 500_000_000, "open-pos-002")
        with self.assertRaises(LimitExceeded):
            service.open_position(u, USDC_POOL, 500_000_000, "open-pos-003")

    def test_replayed_open_does_not_double_spend(self):
        service = self.make()
        u = self.user()
        self.fund(u)
        first = service.open_position(u, USDC_POOL, 500_000_000, "open-pos-001")
        second = service.open_position(u, USDC_POOL, 500_000_000, "open-pos-001")
        self.assertEqual(first["position_id"], second["position_id"])
        self.assertEqual(service.wallet(u)["balances"]["USDT"], 2_500_000_000)

    def test_fees_accrue_with_simulated_time_and_stay_bounded(self):
        service = self.make(sim_time_scale=1440.0)
        u = self.user()
        self.fund(u)
        service.open_position(u, USDC_POOL, 500_000_000, "open-pos-001")
        self.assertEqual(service.list_positions(u)[0]["fees_minor"], 0)
        self.clock.now += 60                                  # one real minute = one simulated day
        first = service.list_positions(u)[0]
        self.assertGreater(first["fees_minor"], 0)
        self.assertAlmostEqual(first["simulated_days"], 1.0, places=1)
        self.clock.now += 3600 * 24 * 400                    # absurdly long: capped by the sanity bound
        later = service.list_positions(u)[0]
        self.assertLessEqual(later["value_minor"], 2 * 500_000_000)
        self.assertGreaterEqual(later["fees_minor"], first["fees_minor"])

    def test_price_halving_values_position_at_sqrt_of_ratio(self):
        service = self.make()
        u = self.user()
        self.fund(u)
        service.open_position(u, ETH_POOL, 200_000_000, "open-pos-001")
        def crash(s):
            return dataclasses.replace(s, price_history=s.price_history[:-1] + (s.price_history[-1] * 0.5,))
        self.provider.overrides[ETH_POOL] = crash
        self.clock.now += 40
        position = service.list_positions(u)[0]
        self.assertAlmostEqual((position["value_minor"] - position["fees_minor"]) / 200_000_000, 0.5 ** 0.5, places=3)

    def test_close_pays_out_and_reconciles(self):
        service = self.make()
        u = self.user()
        self.fund(u)
        pos = service.open_position(u, USDC_POOL, 500_000_000, "open-pos-001")
        self.clock.now += 120
        closed = service.close_position(u, pos["position_id"], "close-key-001")
        self.assertGreater(closed["returned_usdt_micro"], 500_000_000)
        self.assertEqual(service.close_position(u, pos["position_id"], "close-key-001"), closed)
        with self.assertRaises(InvalidOperation):
            service.close_position(u, pos["position_id"], "close-key-002")
        self.assertEqual(service.wallet(u)["open_positions"], 0)
        self.assertTrue(service.ledger.reconcile()["ok"])

    def test_positions_are_private(self):
        service = self.make()
        a, b = self.user("alice"), self.user("bobby")
        self.fund(a)
        pos = service.open_position(a, USDC_POOL, 500_000_000, "open-pos-001")
        self.assertEqual(service.list_positions(b), [])
        with self.assertRaises(PositionNotFound):
            service.close_position(b, pos["position_id"], "close-key-001")
        with self.assertRaises(PositionNotFound):
            service.close_position(b, 12345, "close-key-002")

    def test_deploy_recommendation(self):
        service = self.make()
        u = self.user()
        self.fund(u)
        opened = service.deploy_recommendation(u, 3_000_000_000, "deploy-key-01")
        self.assertGreaterEqual(len(opened), 3)
        again = service.deploy_recommendation(u, 3_000_000_000, "deploy-key-01")
        self.assertEqual([p["position_id"] for p in opened], [p["position_id"] for p in again])
        with self.assertRaises(InsufficientFunds):
            service.deploy_recommendation(u, 3_000_000_000, "deploy-key-02")
        self.assertTrue(service.ledger.reconcile()["ok"])


if __name__ == "__main__":
    unittest.main()
