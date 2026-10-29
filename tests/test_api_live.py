import json
import unittest

from app import sandbox_webhook
from app.fx import RateService, StaticRateSource
from app.accounts import MemoryMailer
from app.live import sign_event
try:
    from tests.api_client import Client, PASSWORD, PIN, RunningServer
except ImportError:
    from api_client import Client, PASSWORD, PIN, RunningServer

SECRET = "w" * 32
GT_ACCOUNT = "0123456785"
LIVE = {"X-Funds-Mode": "live"}
DEMO = {"X-Funds-Mode": "demo"}


def live_server(**kw):
    rates = kw.pop("rates", RateService([StaticRateSource("1500", 100, "a"), StaticRateSource("1502", 100, "b")], max_age_seconds=60))
    return RunningServer(service_kwargs={"live_rate_service": rates}, live_enabled=True, live_rails="sandbox", webhook_secret=SECRET, **kw)


class LiveApiCase(unittest.TestCase):
    def setUp(self):
        self.server = live_server()
        self.addCleanup(self.server.stop)
        self.mailer = self.server.service.accounts.mailer = MemoryMailer()
        self.alice = self.qualified("alice")

    def qualified(self, name):
        c = Client(self.server).register_login(name)
        self.assertEqual(c.post("/api/security/email", {"email": f"{name}@example.com"})[0], 200)
        self.assertEqual(c.post("/api/security/email/verify", {"code": self.mailer.sent[-1][2].split("code is ")[1][:8]})[0], 200)
        c.verify_kyc()
        self.assertEqual(c.post("/api/live/terms", {"pin": PIN})[0], 200)
        return c

    def webhook(self, event, source="payments", **kw):
        body = json.dumps(event).encode()
        return Client(self.server).request("POST", f"/api/webhooks/{source}", raw=body, headers=sign_event(SECRET, body, **kw))

    def fund(self, client, amount=500_000_000, key="live-dep-0001"):
        status, request, _ = client.post("/api/live/deposits", {"amount_minor": amount}, key=key)
        self.assertEqual(status, 200, request)
        status, body, _ = self.webhook({"id": "evt-" + key, "type": "deposit.confirmed", "reference": request["reference"],
                                        "amount_minor": amount, "currency": "NGN"})
        self.assertEqual((status, body["status"]), (200, "applied"))
        return request


class TestModeRouting(LiveApiCase):
    def test_header_selects_the_books_and_is_echoed(self):
        self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 1_000_000}, key=True, headers=DEMO)
        self.fund(self.alice)
        status, demo, headers = self.alice.get("/api/wallet", headers=DEMO)
        self.assertEqual((status, headers["X-Funds-Mode"], demo["balances"]), (200, "demo", {"NGN": 0, "USDT": 1_000_000}))
        status, live, headers = self.alice.get("/api/wallet", headers=LIVE)
        self.assertEqual((status, headers["X-Funds-Mode"], live["balances"]), (200, "live", {"NGN": 500_000_000, "USDT": 0}))
        self.assertEqual(self.alice.get("/api/wallet")[1]["balances"], demo["balances"])               # no header means demo

    def test_invalid_modes_are_rejected_everywhere(self):
        for value in ("LIVE", "real", "demo,live", "live;demo"):
            self.assertEqual(self.alice.get("/api/wallet", headers={"X-Funds-Mode": value})[0], 400, value)
            self.assertEqual(self.alice.get("/api/security", headers={"X-Funds-Mode": value})[0], 400, value)

    def test_unqualified_users_get_the_missing_requirements(self):
        bob = Client(self.server).register_login("bobby")
        status, body, _ = bob.get("/api/wallet", headers=LIVE)
        self.assertEqual((status, body["error"]["code"]), (403, "live_unavailable"))
        missing = {r["key"] for r in body["error"]["requirements"] if not r["ok"]}
        self.assertEqual(missing, {"kyc", "email", "terms"})
        self.assertEqual(bob.get("/api/wallet", headers=DEMO)[0], 200)                                # demo is unaffected

    def test_live_is_unavailable_on_a_server_that_did_not_enable_it(self):
        plain = RunningServer()
        self.addCleanup(plain.stop)
        c = Client(plain).register_login("carol")
        status, body, _ = c.get("/api/wallet", headers=LIVE)
        self.assertEqual((status, body["error"]["code"]), (403, "live_unavailable"))
        status, body, _ = c.get("/api/live/status")
        self.assertEqual((status, body["enabled"], body["available"]), (200, False, False))
        self.assertEqual(Client(plain).request("POST", "/api/webhooks/payments", {})[0], 404)

    def test_simulated_tools_are_blocked_in_live_mode(self):
        self.assertEqual(self.alice.post("/api/wallet/deposits", {"currency": "NGN", "amount_minor": 1_000_000}, key=True, headers=LIVE)[0], 403)
        self.assertEqual(self.alice.post("/api/withdrawals/wd:x/simulate", {"outcome": "complete"}, headers=LIVE)[0], 403)

    def test_live_status_lists_terms_and_rails(self):
        status, body, _ = self.alice.get("/api/live/status")
        self.assertTrue(body["available"])
        self.assertTrue(body["rails"]["sandbox"])
        self.assertGreaterEqual(len(body["terms"]), 3)
        self.assertEqual(self.alice.get("/api/session")[1]["live"], {"enabled": True, "sandbox": True})


class TestLiveDepositsAndPayoutsOverHttp(LiveApiCase):
    def test_signed_webhooks_only(self):
        _, request, _ = self.alice.post("/api/live/deposits", {"amount_minor": 500_000_000}, key="live-dep-0001")
        self.assertEqual(request["status"], "pending")
        self.assertTrue(request["sandbox"])
        event = {"id": "e1", "type": "deposit.confirmed", "reference": request["reference"], "amount_minor": 500_000_000, "currency": "NGN"}
        body = json.dumps(event).encode()
        anon = Client(self.server)
        self.assertEqual(anon.request("POST", "/api/webhooks/payments", raw=body)[0], 401)                           # unsigned
        self.assertEqual(anon.request("POST", "/api/webhooks/payments", raw=body, headers={"X-Timestamp": "1", "X-Signature": "00"})[0], 401)
        self.assertEqual(self.webhook(event, timestamp=1)[0], 401)                                                  # stale
        forged = sign_event("x" * 32, body)
        self.assertEqual(anon.request("POST", "/api/webhooks/payments", raw=body, headers=forged)[0], 401)
        self.assertEqual(self.alice.get("/api/wallet", headers=LIVE)[1]["balances"]["NGN"], 0)
        self.assertEqual(self.webhook(event)[0], 200)
        self.assertEqual(self.webhook(event)[1]["status"], "duplicate")
        self.assertEqual(self.alice.get("/api/wallet", headers=LIVE)[1]["balances"]["NGN"], 500_000_000)

    def test_a_tampered_amount_is_rejected_even_when_signed(self):
        _, request, _ = self.alice.post("/api/live/deposits", {"amount_minor": 500_000_000}, key="live-dep-0001")
        status, body, _ = self.webhook({"id": "e1", "type": "deposit.confirmed", "reference": request["reference"],
                                        "amount_minor": 900_000_000, "currency": "NGN"})
        self.assertEqual((status, body["error"]["code"]), (422, "webhook_rejected"))
        self.assertEqual(self.alice.get("/api/wallet", headers=LIVE)[1]["balances"]["NGN"], 0)

    def test_webhooks_do_not_need_a_session_but_still_check_the_host_and_origin(self):
        body = json.dumps({"id": "e1", "type": "mystery"}).encode()
        headers = sign_event(SECRET, body)
        anon = Client(self.server)
        self.assertEqual(anon.request("POST", "/api/webhooks/payments", raw=body, headers={**headers, "Host": "evil.example"})[0], 400)
        self.assertEqual(anon.request("POST", "/api/webhooks/payments", raw=body, headers={**headers, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(anon.request("POST", "/api/webhooks/payments", raw=body, headers=headers)[0], 422)          # signed but unsupported type

    def test_live_withdrawal_round_trip_with_the_sandbox_rail(self):
        self.fund(self.alice)
        status, w, _ = self.alice.post("/api/withdrawals", {"currency": "NGN", "amount_minor": 4_000_000, "destination": {"account_number": GT_ACCOUNT}},
                                       key="live-wd-0001", headers=LIVE)
        self.assertEqual((status, w["status"]), (201, "processing"))
        self.assertEqual(self.alice.post(f"/api/withdrawals/{w['withdrawal_id']}/cancel", headers=LIVE)[0], 409)      # the rail has it
        self.assertEqual(self.webhook({"id": "p1", "type": "payout.completed", "withdrawal_id": w["withdrawal_id"]})[0], 200)
        done = self.alice.get("/api/withdrawals", headers=LIVE)[1]["withdrawals"][0]
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.alice.get("/api/withdrawals", headers=DEMO)[1]["withdrawals"], [])
        self.assertTrue(self.server.service.books["live"].ledger.reconcile()["ok"])

    def test_the_pin_is_still_required_in_live_mode(self):
        status, body, _ = self.alice.request("POST", "/api/live/deposits", {"amount_minor": 500_000_000}, key=True, pin=False)
        self.assertEqual((status, body["error"]["code"]), (403, "pin_required"))

    def test_live_conversion_needs_two_rate_sources(self):
        server = live_server(rates=RateService([StaticRateSource("1500", 100, "only")], max_age_seconds=60))
        self.addCleanup(server.stop)
        mailer = server.service.accounts.mailer = MemoryMailer()
        c = Client(server).register_login("dora")
        c.post("/api/security/email", {"email": "dora@example.com"})
        c.post("/api/security/email/verify", {"code": mailer.sent[-1][2].split("code is ")[1][:8]})
        c.verify_kyc()
        c.post("/api/live/terms", {"pin": PIN})
        status, body, _ = c.post("/api/quotes", {"source_currency": "NGN", "amount_minor": 100_000_000}, headers=LIVE)
        self.assertEqual((status, body["error"]["code"]), (403, "live_unavailable"))
        self.assertIn("two independent", body["error"]["message"])
        self.assertEqual(c.post("/api/quotes", {"source_currency": "NGN", "amount_minor": 100_000_000}, headers=DEMO)[0], 201)

    def test_audit_trail_marks_which_books_were_used(self):
        self.fund(self.alice)
        events = {e["event"]: e["detail"].get("mode") for e in self.alice.get("/api/activity")[1]["events"]}
        self.assertEqual(events["live_deposit_confirmed"], "live")


class TestSandboxCli(unittest.TestCase):
    def test_events_are_built_and_signed_for_the_server(self):
        args = sandbox_webhook.argparse.Namespace(command="deposit", reference="D1", amount=500, event_id="x1", source="payments",
                                                  withdrawal_id=None, outcome=None)
        self.assertEqual(sandbox_webhook.build_event(args), ("payments", {"id": "x1", "type": "deposit.confirmed", "reference": "D1",
                                                                          "amount_minor": 500, "currency": "NGN"}))
        pay = sandbox_webhook.argparse.Namespace(command="payout", withdrawal_id="wd:1", outcome="failed", source="chain", event_id="")
        source, event = sandbox_webhook.build_event(pay)
        self.assertEqual((source, event["type"], event["withdrawal_id"]), ("chain", "payout.failed", "wd:1"))
        self.assertTrue(event["id"].startswith("sbx-"))

    def test_refuses_to_run_without_the_secret(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"DEX_WEBHOOK_SECRET": ""}):
            self.assertEqual(sandbox_webhook.main(["deposit", "--reference", "D1", "--amount", "5"]), 2)


if __name__ == "__main__":
    unittest.main()
