import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.accounts import MemoryMailer
from app.config import Settings
from app.fx import RateService, StaticRateSource
from app.live import SandboxChainExecutor, SandboxPaymentRail, WebhookError, WebhookRejected, sign_event
from app.ledger import InsufficientFunds, InvalidOperation
from app.service import LiveUnavailable, PlatformService
from app.service import ProviderError, SimulationDisabled, NotFound

SECRET = "w" * 32
PIN = "482915"
PW = "correct-horse-battery"
USDC_POOL = "usdc-usdt-arbitrum-1bp"
ACCOUNT = "0123456785"


def two_rate_sources():
    return RateService([StaticRateSource("1500", 100, "a"), StaticRateSource("1502", 100, "b")], max_age_seconds=60)


class LiveCase(unittest.TestCase):
    def make(self, **kw):
        defaults = dict(database=":memory:", password_iterations=1000, pool_refresh_seconds=0, live_enabled=True,
                        live_rails="sandbox", webhook_secret=SECRET)
        kwargs = {k: kw.pop(k) for k in ("rails", "chain", "live_rate_service") if k in kw}
        self.service = PlatformService(Settings(**{**defaults, **kw}), live_rate_service=kwargs.pop("live_rate_service", two_rate_sources()), **kwargs)
        self.addCleanup(self.service.close)
        self.mailer = self.service.accounts.mailer = MemoryMailer()
        return self.service

    _bvn = iter(range(10 ** 10, 10 ** 10 + 1000))

    def user(self, name="alice", qualify=True):
        service = self.service
        uid = service.accounts.register(name, PW, ip=name, email=f"{name}@example.com")["user_id"]
        if qualify:
            service.accounts.verify_email(uid, self.mailer.sent[-1][2].split("code is ")[1][:8])
            service.accounts.set_pin(uid, PW, PIN)
            service.kyc_basic(uid, "Test Customer", str(next(self._bvn)))
            service.accept_live_terms(uid, PIN)
        return uid

    def post_event(self, source, event, **kw):
        body = json.dumps(event).encode()
        return self.service.handle_webhook(source, sign_event(SECRET, body, **kw), body)

    def fund_live_ngn(self, uid, amount=500_000_000, ref_key="live-dep-0001"):
        request = self.service.live_deposit_request(uid, amount, ref_key, pin=PIN)
        self.post_event("payments", {"id": f"evt-{ref_key}", "type": "deposit.confirmed", "reference": request["reference"],
                                     "amount_minor": amount, "currency": "NGN"})
        return request


class TestGates(LiveCase):
    def test_live_is_off_by_default(self):
        service = PlatformService(Settings(database=":memory:", password_iterations=1000))
        self.addCleanup(service.close)
        uid = service.accounts.register("alice", PW)["user_id"]
        status = service.live_status(uid)
        self.assertFalse(status["enabled"])
        self.assertFalse(status["available"])
        with self.assertRaises(LiveUnavailable):
            with service.use_mode("live", uid):
                pass
        self.assertNotIn("live", service.books)

    def test_each_requirement_blocks_until_met(self):
        service = self.make()
        uid = self.user(qualify=False)
        unmet = lambda: {r["key"] for r in service.live_status(uid)["requirements"] if not r["ok"]}
        self.assertEqual(unmet(), {"kyc", "email", "pin", "terms"})
        service.accounts.verify_email(uid, self.mailer.sent[-1][2].split("code is ")[1][:8])
        service.accounts.set_pin(uid, PW, PIN)
        self.assertEqual(unmet(), {"kyc", "terms"})
        service.kyc_basic(uid, "Test Customer", "12345678901")
        self.assertEqual(unmet(), {"terms"})
        with self.assertRaises(LiveUnavailable) as ctx:
            with service.use_mode("live", uid):
                pass
        self.assertTrue(any(r["key"] == "terms" and not r["ok"] for r in ctx.exception.requirements))
        service.accept_live_terms(uid, PIN)
        self.assertTrue(service.live_status(uid)["available"])
        with service.use_mode("live", uid):
            self.assertEqual(service.mode, "live")

    def test_accepting_terms_needs_the_pin(self):
        service = self.make()
        uid = self.user(qualify=False)
        service.accounts.set_pin(uid, PW, PIN)
        from app.accounts import InvalidPin
        with self.assertRaises(InvalidPin):
            service.accept_live_terms(uid, "000000")

    def test_operator_pause_blocks_everything(self):
        service = self.make(live_paused=True)
        uid = self.user()
        status = service.live_status(uid)
        self.assertFalse(status["available"])
        self.assertTrue(any(r["key"] == "not_paused" and not r["ok"] for r in status["requirements"]))
        with self.assertRaises(LiveUnavailable):
            service.live_deposit_request(uid, 500_000, "live-dep-0001", pin=PIN)

    def test_a_ledger_that_fails_its_integrity_check_blocks_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "platform.sqlite3")
            first = self.make(database=db)
            first.books["live"].ledger.db.execute("INSERT INTO operations VALUES ('bad','bad','x','{}','{}',CURRENT_TIMESTAMP)")
            first.books["live"].ledger.db.execute("INSERT INTO ledger_entries(operation_id, account, currency, amount_minor) VALUES ('bad','u1','NGN',7)")
            first.close()
            second = PlatformService(Settings(database=db, password_iterations=1000, live_enabled=True, live_rails="sandbox", webhook_secret=SECRET))
            self.addCleanup(second.close)
            self.assertFalse(second.live_healthy)
            self.assertTrue(any(r["key"] == "healthy" and not r["ok"] for r in second.live_requirements()))

    def test_mode_is_validated_and_defaults_to_demo(self):
        service = self.make()
        self.assertEqual(service.mode, "demo")
        for bad in ("LIVE", "real", None, ""):
            with self.assertRaises(InvalidOperation):
                with service.use_mode(bad):
                    pass

    def test_a_failure_inside_live_mode_still_returns_to_demo(self):
        service = self.make()
        uid = self.user()
        with self.assertRaises(RuntimeError):
            with service.use_mode("live", uid):
                raise RuntimeError("boom")
        self.assertEqual(service.mode, "demo")

    def test_other_threads_stay_in_demo_while_one_is_in_live(self):
        service = self.make()
        uid = self.user()
        seen, ready, release = [], threading.Event(), threading.Event()

        def in_live():
            with service.use_mode("live", uid):
                ready.set()
                release.wait(2)

        thread = threading.Thread(target=in_live)
        thread.start()
        ready.wait(2)
        seen.append(service.mode)
        release.set()
        thread.join()
        self.assertEqual(seen, ["demo"])


class TestIsolation(LiveCase):
    def test_demo_and_live_books_are_separate_ledgers(self):
        service = self.make()
        uid = self.user()
        service.deposit(uid, "USDT", 1_000_000_000, "demo-dep-0001", pin=PIN)
        self.fund_live_ngn(uid)
        self.assertEqual(service.wallet(uid)["balances"], {"NGN": 0, "USDT": 1_000_000_000})
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["balances"], {"NGN": 500_000_000, "USDT": 0})
        self.assertIsNot(service.books["demo"].ledger, service.books["live"].ledger)
        self.assertTrue(service.books["demo"].ledger.reconcile()["ok"] and service.books["live"].ledger.reconcile()["ok"])

    def test_simulated_tools_do_not_exist_in_live_mode(self):
        service = self.make()
        uid = self.user()
        with service.use_mode("live", uid):
            with self.assertRaises(SimulationDisabled):
                service.deposit(uid, "NGN", 1_000_000, "sim-dep-0001", pin=PIN)
            with self.assertRaises(SimulationDisabled):
                service.simulate_payout(uid, "wd:x", "complete")

    def test_audit_entries_record_the_funds_mode(self):
        service = self.make()
        uid = self.user()
        service.deposit(uid, "USDT", 1_000_000, "demo-dep-0001", pin=PIN)
        self.fund_live_ngn(uid)
        modes = {e["event"]: e["detail"].get("mode") for e in service.activity(uid, 50)}
        self.assertEqual(modes["deposit_simulated"], "demo")
        self.assertEqual(modes["live_deposit_confirmed"], "live")


class TestLiveDeposits(LiveCase):
    def test_deposit_is_credited_only_after_the_signed_webhook(self):
        service = self.make()
        uid = self.user()
        request = service.live_deposit_request(uid, 500_000_000, "live-dep-0001", pin=PIN)
        self.assertEqual((request["status"], request["sandbox"]), ("pending", True))
        self.assertEqual(request["instructions"]["reference"], request["reference"])
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["balances"]["NGN"], 0)            # not spendable while pending
        self.assertEqual(self.post_event("payments", {"id": "e1", "type": "deposit.confirmed", "reference": request["reference"],
                                                      "amount_minor": 500_000_000, "currency": "NGN"}), {"status": "applied"})
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["balances"]["NGN"], 500_000_000)

    def test_replays_and_duplicate_events_never_double_credit(self):
        service = self.make()
        uid = self.user()
        first = service.live_deposit_request(uid, 500_000_000, "live-dep-0001", pin=PIN)
        again = service.live_deposit_request(uid, 500_000_000, "live-dep-0001", pin=PIN)
        self.assertEqual(first["reference"], again["reference"])
        event = {"id": "e1", "type": "deposit.confirmed", "reference": first["reference"], "amount_minor": 500_000_000, "currency": "NGN"}
        self.assertEqual(self.post_event("payments", event), {"status": "applied"})
        self.assertEqual(self.post_event("payments", event), {"status": "duplicate"})
        self.post_event("payments", {**event, "id": "e2"})                          # a different event id for the same deposit
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["balances"]["NGN"], 500_000_000)

    def test_mismatched_unknown_and_unsigned_events_are_refused(self):
        service = self.make()
        uid = self.user()
        ref = service.live_deposit_request(uid, 500_000_000, "live-dep-0001", pin=PIN)["reference"]
        good = {"id": "e1", "type": "deposit.confirmed", "reference": ref, "amount_minor": 500_000_000, "currency": "NGN"}
        for bad in ({**good, "amount_minor": 900_000_000}, {**good, "currency": "USDT"}, {**good, "reference": "DNOSUCHREF"}):
            with self.assertRaises(WebhookRejected):
                self.post_event("payments", bad)
        body = json.dumps(good).encode()
        with self.assertRaises(WebhookError):
            service.handle_webhook("payments", {"X-Timestamp": "1", "X-Signature": "0" * 64}, body)
        with self.assertRaises(WebhookError):
            service.handle_webhook("payments", {}, body)
        with self.assertRaises(WebhookError):
            self.post_event("payments", good, timestamp=1)                           # signed correctly but years old
        forged = sign_event("x" * 32, body)
        with self.assertRaises(WebhookError):
            service.handle_webhook("payments", forged, body)
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["balances"]["NGN"], 0)

    def test_event_types_are_tied_to_their_source(self):
        service = self.make()
        uid = self.user()
        ref = service.live_deposit_request(uid, 500_000_000, "live-dep-0001", pin=PIN)["reference"]
        with self.assertRaises(WebhookRejected):                                     # chain events cannot confirm NGN deposits
            self.post_event("chain", {"id": "e1", "type": "deposit.confirmed", "reference": ref, "amount_minor": 500_000_000, "currency": "NGN"})
        with self.assertRaises(WebhookRejected):
            self.post_event("payments", {"id": "e2", "type": "mystery.event"})
        with self.assertRaises(NotFound):
            service.handle_webhook("other", {}, b"{}")

    def test_deposit_request_rules(self):
        service = self.make()
        uid = self.user()
        with self.assertRaises(InvalidOperation):
            service.live_deposit_request(uid, 1_000, "live-dep-0001", pin=PIN)
        from app.accounts import InvalidPin
        with self.assertRaises(InvalidPin):
            service.live_deposit_request(uid, 500_000_000, "live-dep-0002", pin="000000")

    def test_webhooks_are_unavailable_without_live_mode(self):
        service = PlatformService(Settings(database=":memory:", password_iterations=1000))
        self.addCleanup(service.close)
        with self.assertRaises(NotFound):
            service.handle_webhook("payments", {}, b"{}")


class FailingRail(SandboxPaymentRail):
    def send_payout(self, withdrawal):
        raise RuntimeError("bank is down")


class FailingChain(SandboxChainExecutor):
    def __init__(self, *a):
        super().__init__(*a)
        self.fail_open = self.fail_close = False

    def open_position(self, pool, amount_minor, reference):
        if self.fail_open:
            raise RuntimeError("rpc down")
        return super().open_position(pool, amount_minor, reference)

    def close_position(self, external_ref):
        if self.fail_close:
            raise RuntimeError("rpc down")
        return super().close_position(external_ref)


class TestLiveWithdrawals(LiveCase):
    def request(self, uid, amount=4_000_000, key="live-wd-0001"):
        with self.service.use_mode("live", uid):
            return self.service.request_withdrawal(uid, "NGN", amount, {"account_number": ACCOUNT}, key, pin=PIN)

    def test_withdrawal_goes_to_the_rail_and_completes_by_webhook(self):
        service = self.make()
        uid = self.user()
        self.fund_live_ngn(uid)
        w = self.request(uid)
        self.assertEqual(w["status"], "processing")
        self.assertTrue(w["provider_ref"].startswith("sbx-pay-"))
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["pending_withdrawals"]["NGN"], 4_000_000 + 5_000)
        self.assertEqual(self.post_event("payments", {"id": "p1", "type": "payout.completed", "withdrawal_id": w["withdrawal_id"]}), {"status": "applied"})
        self.assertEqual(self.post_event("payments", {"id": "p1", "type": "payout.completed", "withdrawal_id": w["withdrawal_id"]}), {"status": "duplicate"})
        with service.use_mode("live", uid):
            self.assertEqual(service.withdrawals(uid)[0]["status"], "completed")
            self.assertEqual(service.ledger.balance("fees", "NGN"), 5_000)
            self.assertTrue(service.ledger.reconcile()["ok"])

    def test_a_failed_payout_returns_the_funds(self):
        service = self.make()
        uid = self.user()
        self.fund_live_ngn(uid)
        w = self.request(uid)
        self.post_event("payments", {"id": "p1", "type": "payout.failed", "withdrawal_id": w["withdrawal_id"], "reason": "account closed"})
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["balances"]["NGN"], 500_000_000)
            self.assertEqual(service.withdrawals(uid)[0]["status"], "failed")

    def test_rail_errors_release_the_hold(self):
        service = self.make(rails=FailingRail(SECRET.encode()), chain=SandboxChainExecutor(SECRET.encode()))
        uid = self.user()
        self.fund_live_ngn(uid)
        with self.assertRaises(ProviderError):
            self.request(uid)
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["balances"]["NGN"], 500_000_000)
            self.assertEqual(service.wallet(uid)["pending_withdrawals"]["NGN"], 0)
            self.assertTrue(service.ledger.reconcile()["ok"])

    def test_payout_events_cannot_touch_the_wrong_withdrawal(self):
        service = self.make()
        uid = self.user()
        self.fund_live_ngn(uid)
        w = self.request(uid)
        with self.assertRaises(WebhookRejected):
            self.post_event("chain", {"id": "c1", "type": "payout.completed", "withdrawal_id": w["withdrawal_id"]})      # NGN is not the chain's
        with self.assertRaises(WebhookRejected):
            self.post_event("payments", {"id": "c2", "type": "payout.completed", "withdrawal_id": "wd:nothing"})

    def test_cannot_withdraw_more_than_the_live_balance_and_demo_funds_do_not_count(self):
        service = self.make()
        uid = self.user()
        service.deposit(uid, "NGN", 500_000_000, "demo-dep-0001", pin=PIN)          # demo funds only
        with self.assertRaises(InsufficientFunds):
            self.request(uid)

    def test_tier_limits_apply_in_live_mode_too(self):
        service = self.make()
        uid = self.user(qualify=False)
        service.accounts.verify_email(uid, self.mailer.sent[-1][2].split("code is ")[1][:8])
        service.accounts.set_pin(uid, PW, PIN)
        service.kyc_basic(uid, "Test Customer", "12345678909")
        service.accept_live_terms(uid, PIN)
        self.fund_live_ngn(uid, 5_000_000_000)
        from app.ledger import LimitExceeded
        with self.assertRaises(LimitExceeded):
            self.request(uid, amount=200_000_000)                                      # tier 1 allows NGN 1,000,000 each


class TestLiveConversionsAndPositions(LiveCase):
    def test_live_conversion_needs_two_rate_sources_and_uses_the_live_book(self):
        one = self.make(live_rate_service=RateService([StaticRateSource("1500", 100, "only")], max_age_seconds=60))
        uid = self.user()
        self.fund_live_ngn(uid)
        with one.use_mode("live", uid):
            with self.assertRaises(LiveUnavailable):
                one.create_quote(uid, "NGN", 100_000_000)

    def test_conversion_with_two_sources_books_fees_in_live_only(self):
        service = self.make()
        uid = self.user()
        self.fund_live_ngn(uid)
        with service.use_mode("live", uid):
            quote = service.create_quote(uid, "NGN", 150_000_000)
            service.execute_quote(uid, quote["quote_id"], "live-conv-0001", pin=PIN)
            self.assertEqual(service.ledger.balance("fees", "USDT"), quote["fee_minor"])
            self.assertGreater(service.wallet(uid)["balances"]["USDT"], 0)
        self.assertEqual(service.books["demo"].ledger.balance("fees", "USDT"), 0)

    def open_live_position(self, uid, key="live-pos-0001", amount=500_000_000):
        with self.service.use_mode("live", uid):
            return self.service.open_position(uid, USDC_POOL, amount, key, pin=PIN)

    def test_live_positions_go_through_the_chain_executor(self):
        service = self.make()
        uid = self.user()
        service.books["live"].ledger.deposit(uid, "USDT", 2_000_000_000, "seed-usdt-live")            # test-only seed
        position = self.open_live_position(uid)
        row = service.books["live"].positions.get_position(position["position_id"])
        self.assertTrue(row["external_ref"].startswith("sbx-pos-"))
        with service.use_mode("live", uid):
            service.close_position(uid, position["position_id"], "live-close-0001", pin=PIN)
            self.assertEqual(service.wallet(uid)["open_positions"], 0)
            self.assertTrue(service.ledger.reconcile()["ok"])

    def test_chain_failures_never_leave_funds_stuck(self):
        chain = FailingChain(SECRET.encode())
        service = self.make(rails=SandboxPaymentRail(SECRET.encode()), chain=chain)
        uid = self.user()
        service.books["live"].ledger.deposit(uid, "USDT", 2_000_000_000, "seed-usdt-live")
        chain.fail_open = True
        with self.assertRaises(ProviderError):
            self.open_live_position(uid)
        with service.use_mode("live", uid):
            self.assertEqual(service.wallet(uid)["balances"]["USDT"], 2_000_000_000)           # refunded in full
            self.assertTrue(service.ledger.reconcile()["ok"])
        chain.fail_open = False
        position = self.open_live_position(uid, key="live-pos-0002")
        chain.fail_close = True
        with service.use_mode("live", uid):
            with self.assertRaises(RuntimeError):
                service.close_position(uid, position["position_id"], "live-close-0001", pin=PIN)
            self.assertEqual(service.wallet(uid)["open_positions"], 1)                          # still open, still tracked

    def test_one_click_allocation_is_not_available_in_live_mode(self):
        service = self.make()
        uid = self.user()
        service.books["live"].ledger.deposit(uid, "USDT", 2_000_000_000, "seed-usdt-live")
        with service.use_mode("live", uid):
            with self.assertRaises(LiveUnavailable):
                service.deploy_recommendation(uid, 1_000_000_000, "live-deploy-001", pin=PIN)


class TestSettingsAndAdapters(unittest.TestCase):
    def test_live_settings_fail_closed(self):
        for kw in ({"live_enabled": True}, {"live_enabled": True, "live_rails": "sandbox"}, {"live_rails": "acme-bank"},
                   {"live_enabled": True, "live_rails": "sandbox", "webhook_secret": "short"}):
            with self.assertRaises(ValueError, msg=str(kw)):
                Settings(**kw)
        with self.assertRaises(ValueError):
            from app.live import build_adapters
            build_adapters("acme-bank", SECRET)

    def test_signature_scheme(self):
        rail = SandboxPaymentRail(SECRET.encode())
        body = b'{"id":"1","type":"x"}'
        headers = sign_event(SECRET, body, timestamp=1_000)
        self.assertEqual(rail.verify_webhook(headers, body, 1_100)["id"], "1")
        with self.assertRaises(WebhookError):
            rail.verify_webhook(headers, body + b" ", 1_100)                                   # body changed
        with self.assertRaises(WebhookError):
            rail.verify_webhook(headers, body, 1_000 + 301)                                    # too old
        with self.assertRaises(WebhookError):
            rail.verify_webhook({"X-Timestamp": "abc", "X-Signature": "x"}, body, 1_000)
        with self.assertRaises(ValueError):
            SandboxPaymentRail(b"short")
        for bad_body in (b"not json", b"[]", b'{"id": 1, "type": "x"}'):
            with self.assertRaises(WebhookError):
                rail.verify_webhook(sign_event(SECRET, bad_body, timestamp=1_000), bad_body, 1_000)


if __name__ == "__main__":
    unittest.main()
