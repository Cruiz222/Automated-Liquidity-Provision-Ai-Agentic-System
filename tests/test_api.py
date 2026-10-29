import json
import re
import tempfile
import unittest
from pathlib import Path

from app.accounts import RateLimiter
try:                                    # works for both `discover -s tests` and `discover -s tests -t .`
    from tests.api_client import Client, PASSWORD, RunningServer
except ImportError:
    from api_client import Client, PASSWORD, RunningServer

STATIC = Path(__file__).resolve().parent.parent / "app" / "api" / "static"
BANK = {"bank_code": "058", "account_number": "0123456785"}
USDC_POOL = "usdc-usdt-arbitrum-1bp"


class ApiCase(unittest.TestCase):
    server_args: dict = {}

    def setUp(self):
        self.server = RunningServer(**self.server_args)
        self.addCleanup(self.server.stop)
        self.alice = Client(self.server).register_login("alice")

    def anon(self):
        return Client(self.server)


class TestFlows(ApiCase):
    def test_dashboard_assets_are_served_with_security_headers(self):
        status, body, headers = self.anon().get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"SIMULATION ONLY", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for path, kind in (("/static/app.js", "javascript"), ("/static/app.css", "css")):
            status, _, headers = self.anon().get(path)
            self.assertEqual(status, 200)
            self.assertIn(kind, headers["Content-Type"])

    def test_login_sets_a_hardened_cookie_and_never_returns_the_token(self):
        client = self.anon()
        client.request("POST", "/api/auth/register", {"username": "bobby", "password": PASSWORD})
        status, body, headers = client.request("POST", "/api/auth/login", {"username": "bobby", "password": PASSWORD})
        cookie = headers["Set-Cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age="):
            self.assertIn(part, cookie)
        self.assertNotIn("Secure", cookie)                      # off on plain-HTTP localhost by default
        self.assertNotIn(cookie.split(";")[0].split("=", 1)[1], json.dumps(body))
        self.assertEqual(self.alice.get("/api/session")[1]["user"]["username"], "alice")

    def test_secure_flag_when_configured(self):
        server = RunningServer(secure_cookies=True)
        self.addCleanup(server.stop)
        client = Client(server)
        client.request("POST", "/api/auth/register", {"username": "bobby", "password": PASSWORD})
        _, _, headers = client.request("POST", "/api/auth/login", {"username": "bobby", "password": PASSWORD},
                                       headers={"Origin": f"https://127.0.0.1:{server.port}"})
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_full_customer_journey(self):
        a = self.alice.verify_kyc()
        self.assertEqual(a.post("/api/wallet/deposits", {"currency": "NGN", "amount_minor": 300_000_000}, key=True)[0], 200)
        status, quote, _ = a.post("/api/quotes", {"source_currency": "NGN", "amount_minor": 200_000_000})
        self.assertEqual(status, 201)
        self.assertEqual(set(quote) >= {"quote_id", "rate", "fee_minor", "expires_at", "target_amount_minor", "rate_source"}, True)
        self.assertEqual(a.post(f"/api/quotes/{quote['quote_id']}/execute", key=True)[0], 200)
        wallet = a.get("/api/wallet")[1]
        self.assertEqual(wallet["balances"]["NGN"], 100_000_000)
        usdt = wallet["balances"]["USDT"]
        self.assertGreater(usdt, 1_000_000_000)
        self.assertEqual(a.get(f"/api/recommendation?amount_minor={usdt}")[0], 200)
        status, deployed, _ = a.post("/api/positions/deploy", {"amount_minor": usdt}, key=True)
        self.assertEqual(status, 200)
        positions = a.get("/api/positions")[1]["positions"]
        self.assertEqual(len(positions), len(deployed["positions"]))
        self.assertEqual(a.post(f"/api/positions/{positions[0]['position_id']}/close", key=True)[0], 200)
        status, w, _ = a.post("/api/withdrawals", {"currency": "NGN", "amount_minor": 10_000_000, "destination": BANK}, key=True)
        self.assertEqual((status, w["status"]), (201, "pending"))
        done = a.post(f"/api/withdrawals/{w['withdrawal_id']}/simulate", {"outcome": "complete"})[1]
        self.assertEqual(done["status"], "completed")
        history = a.get("/api/wallet/history?limit=5")[1]
        self.assertEqual(len(history["entries"]), 5)
        self.assertIsNotNone(history["next_before"])
        events = [e["event"] for e in a.get("/api/activity")[1]["events"]]
        self.assertIn("conversion_executed", events)
        self.assertTrue(self.server.service.ledger.reconcile()["ok"])

    def test_pool_scenario_endpoint(self):
        status, body, _ = self.alice.get("/api/pools/weth-usdc-arbitrum-5bp/scenario?price_change_pct=-50&days=30")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["impermanent_loss_pct"], -5.719, places=2)
        self.assertEqual(self.alice.get("/api/pools/nope/scenario")[0], 404)
        self.assertEqual(self.alice.get("/api/pools/weth-usdc-arbitrum-5bp/scenario?price_change_pct=abc")[0], 400)

    def test_unknown_routes_and_methods(self):
        self.assertEqual(self.alice.get("/api/nothing")[0], 404)
        self.assertEqual(self.alice.request("PUT", "/api/wallet", {})[0], 405)
        self.assertEqual(self.alice.request("DELETE", "/api/withdrawals", {})[0], 405)
        status, _, headers = self.anon().request("OPTIONS", "/api/wallet")
        self.assertEqual(status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", headers)


class TestAuthenticationAndSessions(ApiCase):
    def test_every_protected_route_requires_a_session(self):
        guest = self.anon()
        for path in ("/api/session", "/api/wallet", "/api/wallet/history", "/api/withdrawals", "/api/pools",
                     "/api/recommendation?amount_minor=100", "/api/positions", "/api/activity",
                     "/api/pools/x/scenario"):
            self.assertEqual(guest.get(path)[0], 401, path)
        for path in ("/api/wallet/deposits", "/api/withdrawals", "/api/quotes", "/api/positions", "/api/positions/deploy",
                     "/api/auth/logout", "/api/auth/logout-all", "/api/auth/change-password",
                     "/api/quotes/abc/execute", "/api/positions/abc/close", "/api/withdrawals/abc/cancel"):
            self.assertEqual(guest.post(path, {}, key=True)[0], 401, path)

    def test_garbage_and_forged_cookies_are_rejected(self):
        for cookie in ("nope", "x" * 300, "../../etc/passwd" * 4, ""):
            c = self.anon()
            c.cookie = cookie
            self.assertEqual(c.get("/api/wallet")[0], 401)
        forged = self.anon()
        forged.cookie = self.alice.cookie[:-1] + ("A" if self.alice.cookie[-1] != "A" else "B")
        self.assertEqual(forged.get("/api/wallet")[0], 401)

    def test_login_errors_do_not_reveal_which_part_was_wrong(self):
        c = self.anon()
        wrong = c.request("POST", "/api/auth/login", {"username": "alice", "password": "wrong-password-1"})
        unknown = c.request("POST", "/api/auth/login", {"username": "nobody", "password": PASSWORD})
        self.assertEqual((wrong[0], wrong[1]), (unknown[0], unknown[1]))
        self.assertEqual(wrong[0], 401)

    def test_lockout_returns_429_with_retry_after_even_for_the_right_password(self):
        c = self.anon()
        for _ in range(5):
            self.assertEqual(c.request("POST", "/api/auth/login", {"username": "alice", "password": "wrong-password-1"})[0], 401)
        status, _, headers = c.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD})
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)

    def test_reserved_and_malformed_usernames_are_refused(self):
        c = self.anon()
        for name in ("clearing", "liquidity-clearing", "fees", "withdrawal-hold", "admin", "a b", "x"):
            self.assertEqual(c.request("POST", "/api/auth/register", {"username": name, "password": PASSWORD})[0], 400, name)

    def test_logout_revokes_the_session_and_clears_the_cookie(self):
        status, _, headers = self.alice.post("/api/auth/logout")
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.alice.get("/api/wallet")[0], 401)

    def test_password_change_revokes_other_sessions(self):
        other = self.anon()
        _, body, headers = other.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD})
        other.cookie, other.csrf = headers["Set-Cookie"].split(";")[0].split("=", 1)[1], body["csrf_token"]
        self.assertEqual(self.alice.post("/api/auth/change-password",
                                         {"current_password": PASSWORD, "new_password": "a-brand-new-password"})[0], 200)
        self.assertEqual(other.get("/api/wallet")[0], 401)
        self.assertEqual(self.alice.get("/api/wallet")[0], 200)
        self.assertEqual(self.alice.post("/api/auth/change-password",
                                         {"current_password": "wrong-password-1", "new_password": "yet-another-password"})[0], 401)


class TestRequestHardening(ApiCase):
    def test_csrf_token_is_required_and_checked(self):
        body = {"currency": "USDT", "amount_minor": 1_000_000}
        self.assertEqual(self.alice.request("POST", "/api/wallet/deposits", body, key=True, csrf=False)[0], 403)
        self.assertEqual(self.alice.request("POST", "/api/wallet/deposits", body, key=True,
                                            headers={"X-CSRF-Token": "wrong-token"})[0], 403)
        self.assertEqual(self.alice.request("POST", "/api/wallet/deposits", body, key=True)[0], 200)

    def test_csrf_token_of_another_user_does_not_work(self):
        mallory = Client(self.server).register_login("mallory")
        self.assertEqual(self.alice.request("POST", "/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100},
                                            key=True, headers={"X-CSRF-Token": mallory.csrf})[0], 403)

    def test_dns_rebinding_host_header_is_rejected(self):
        for host in ("evil.example", f"evil.example:{self.server.port}", "127.0.0.1:1", ""):
            self.assertEqual(self.alice.get("/api/wallet", headers={"Host": host})[0], 400, host)
            self.assertEqual(self.anon().get("/", headers={"Host": host})[0], 400, host)
        self.assertEqual(self.alice.get("/api/wallet", headers={"Host": f"localhost:{self.server.port}"})[0], 200)

    def test_cross_origin_requests_are_rejected(self):
        evil = {"Origin": "http://evil.example"}
        self.assertEqual(self.alice.get("/api/wallet", headers=evil)[0], 403)
        self.assertEqual(self.anon().request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD}, headers=evil)[0], 403)
        self.assertEqual(self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100}, key=True, headers=evil)[0], 403)
        self.assertEqual(self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100}, key=True,
                                         headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100}, key=True,
                                         headers={"Origin": f"http://127.0.0.1:{self.server.port}", "Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_body_rules(self):
        c = self.alice
        self.assertEqual(c.request("POST", "/api/quotes", raw=b'{"source_currency":"NGN"', key=False)[0], 400)           # truncated JSON
        self.assertEqual(c.request("POST", "/api/quotes", raw=b"[1,2]")[0], 400)                                         # not an object
        self.assertEqual(c.request("POST", "/api/quotes", raw=b"\xff\xfe")[0], 400)                                       # not UTF-8
        self.assertEqual(c.request("POST", "/api/quotes", {"source_currency": "NGN", "amount_minor": 1_000_000},
                                   headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(c.request("POST", "/api/quotes", raw=b"x" * 20_000)[0], 413)
        self.assertEqual(c.request("POST", "/api/quotes", {"source_currency": "NGN", "amount_minor": 1_000_000},
                                   headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_strict_field_and_type_validation(self):
        c = self.alice
        for body in ({"source_currency": "NGN"}, {"source_currency": "NGN", "amount_minor": 1_000_000, "extra": 1},
                     {"source_currency": "NGN", "amount_minor": "1000000"}, {"source_currency": "NGN", "amount_minor": 1.5},
                     {"source_currency": "NGN", "amount_minor": True}, {"source_currency": ["NGN"], "amount_minor": 1_000_000},
                     {"source_currency": "BTC", "amount_minor": 1_000_000}, {"source_currency": "NGN", "amount_minor": -5}):
            self.assertEqual(c.post("/api/quotes", body)[0], 400, body)

    def test_customers_cannot_supply_valuations_or_ledger_fields(self):
        self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 5_000_000_000}, key=True)
        status, _, _ = self.alice.post("/api/positions", {"pool_id": USDC_POOL, "amount_minor": 500_000_000, "value_minor": 10 ** 12}, key=True)
        self.assertEqual(status, 400)
        for field in ("account", "user_id", "fee_minor", "rate"):
            self.assertEqual(self.alice.post("/api/quotes", {"source_currency": "NGN", "amount_minor": 1_000_000, field: "x"})[0], 400)

    def test_withdrawal_destination_is_validated(self):
        self.alice.post("/api/wallet/deposits", {"currency": "NGN", "amount_minor": 100_000_000}, key=True)
        for dest in ({"bank_code": "058"}, {"bank_code": "058", "account_number": "12"}, "string", {"a": 1}, {"x": "y"} | {str(i): "z" for i in range(6)}):
            self.assertEqual(self.alice.post("/api/withdrawals", {"currency": "NGN", "amount_minor": 10_000_000, "destination": dest}, key=True)[0], 400)

    def test_query_parameters_are_bounded(self):
        for path in ("/api/wallet/history?limit=0", "/api/wallet/history?limit=201", "/api/wallet/history?limit=abc",
                     "/api/wallet/history?before=-1", "/api/activity?limit=500", "/api/recommendation?amount_minor=0"):
            self.assertEqual(self.alice.get(path)[0], 400, path)

    def test_static_files_use_a_fixed_allowlist(self):
        for path in ("/static/../../etc/passwd", "/static/app.js/", "/app/api/server.py", "/static/%2e%2e/server.py",
                     "/static/index.html", "/.git/config", "//etc/passwd"):
            self.assertIn(self.anon().get(path)[0], (400, 404), path)

    def test_internal_errors_are_generic(self):
        self.server.service.wallet = lambda user_id: (_ for _ in ()).throw(RuntimeError("super secret database detail"))
        with self.assertLogs("app.api", level="ERROR") as logs:
            status, body, _ = self.alice.get("/api/wallet")
        self.assertEqual(status, 500)
        self.assertNotIn("secret", " ".join(logs.output).split("Traceback")[0])          # the log line itself stays terse
        text = json.dumps(body)
        self.assertNotIn("secret", text)
        self.assertNotIn("Traceback", text)
        self.assertIn("reference", text)

    def test_ip_rate_limit(self):
        self.server.server.api.ip_limiter = RateLimiter(5, 60)
        statuses = [self.anon().get("/api/health")[0] for _ in range(8)]
        self.assertEqual(statuses[:5], [200] * 5)
        self.assertIn(429, statuses[5:])

    def test_write_rate_limit_is_per_user(self):
        self.server.server.api.write_limiter = RateLimiter(3, 60)
        results = [self.alice.post("/api/quotes", {"source_currency": "NGN", "amount_minor": 1_000_000})[0] for _ in range(6)]
        self.assertIn(429, results)
        other = Client(self.server).register_login("mallory")
        self.assertEqual(other.post("/api/quotes", {"source_currency": "NGN", "amount_minor": 1_000_000})[0], 201)


class TestMoneyEndpointSafety(ApiCase):
    def test_idempotency_key_is_required_and_validated(self):
        body = {"currency": "USDT", "amount_minor": 1_000_000}
        self.assertEqual(self.alice.post("/api/wallet/deposits", body)[0], 400)
        for key in ("short", "has space here", "x" * 101, "semi;colon-key"):
            self.assertEqual(self.alice.post("/api/wallet/deposits", body, key=key)[0], 400, key)

    def test_replays_credit_once_and_conflicts_are_detected(self):
        body = {"currency": "USDT", "amount_minor": 1_000_000}
        first = self.alice.post("/api/wallet/deposits", body, key="replay-key-0001")
        second = self.alice.post("/api/wallet/deposits", body, key="replay-key-0001")
        self.assertEqual((first[0], first[1]), (second[0], second[1]))
        self.assertEqual(self.alice.get("/api/wallet")[1]["balances"]["USDT"], 1_000_000)
        self.assertEqual(self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 2_000_000}, key="replay-key-0001")[0], 409)

    def test_idempotency_keys_are_private_to_each_user(self):
        mallory = Client(self.server).register_login("mallory")
        self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 1_000_000}, key="shared-key-0001")
        status, _, _ = mallory.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 7_000_000}, key="shared-key-0001")
        self.assertEqual(status, 200)                              # not blocked, not merged, not leaked
        self.assertEqual(mallory.get("/api/wallet")[1]["balances"]["USDT"], 7_000_000)
        self.assertEqual(self.alice.get("/api/wallet")[1]["balances"]["USDT"], 1_000_000)

    def test_other_users_cannot_touch_my_resources(self):
        a = self.alice.verify_kyc()
        a.post("/api/wallet/deposits", {"currency": "NGN", "amount_minor": 200_000_000}, key=True)
        a.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 2_000_000_000}, key=True)
        withdrawal = a.post("/api/withdrawals", {"currency": "NGN", "amount_minor": 10_000_000, "destination": BANK}, key=True)[1]
        quote = a.post("/api/quotes", {"source_currency": "NGN", "amount_minor": 100_000_000})[1]
        position = a.post("/api/positions", {"pool_id": USDC_POOL, "amount_minor": 500_000_000}, key=True)[1]
        m = Client(self.server).register_login("mallory")
        wid, qid, pid = withdrawal["withdrawal_id"], quote["quote_id"], position["position_id"]
        self.assertEqual(m.post(f"/api/withdrawals/{wid}/cancel")[0], 404)
        self.assertEqual(m.post(f"/api/withdrawals/{wid}/simulate", {"outcome": "complete"})[0], 404)
        self.assertEqual(m.post(f"/api/quotes/{qid}/execute", key=True)[0], 400)
        self.assertEqual(m.post(f"/api/positions/{pid}/close", key=True)[0], 404)
        for path, key in (("/api/withdrawals", "withdrawals"), ("/api/positions", "positions")):
            self.assertEqual(m.get(path)[1][key], [])
        self.assertEqual(m.get("/api/wallet/history")[1]["entries"], [])
        self.assertEqual(m.get("/api/wallet")[1]["balances"], {"NGN": 0, "USDT": 0})
        self.assertEqual(a.get("/api/wallet")[1]["balances"]["NGN"], 190_000_000 - 5_000)    # 200M - 10M withdrawal - 5k fee; untouched by mallory

    def test_simulation_endpoints_can_be_switched_off(self):
        server = RunningServer(simulation_mode=False)
        self.addCleanup(server.stop)
        c = Client(server).register_login("bobby")
        self.assertEqual(c.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100}, key=True)[0], 403)
        self.assertEqual(c.get("/api/wallet")[0], 200)

    def test_positions_need_real_funds(self):
        status, body, _ = self.alice.post("/api/positions", {"pool_id": USDC_POOL, "amount_minor": 10 ** 12}, key=True)
        self.assertEqual(status, 422)


class TestPersistenceAcrossRestart(unittest.TestCase):
    def test_restart_does_not_hand_an_existing_wallet_to_a_new_registrant(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "platform.sqlite3")
            first = RunningServer(database=db)
            bob = Client(first).register_login("bobby")
            bob.post("/api/wallet/deposits", {"currency": "NGN", "amount_minor": 9_000_000}, key=True)
            first.stop()
            second = RunningServer(database=db)
            self.addCleanup(second.stop)
            attacker = Client(second)
            self.assertEqual(attacker.request("POST", "/api/auth/register", {"username": "bobby", "password": "attacker-password-1"})[0], 409)
            self.assertEqual(attacker.request("POST", "/api/auth/login", {"username": "bobby", "password": "attacker-password-1"})[0], 401)
            status, body, headers = attacker.request("POST", "/api/auth/login", {"username": "bobby", "password": PASSWORD})
            attacker.cookie, attacker.csrf = headers["Set-Cookie"].split(";")[0].split("=", 1)[1], body["csrf_token"]
            self.assertEqual(attacker.get("/api/wallet")[1]["balances"]["NGN"], 9_000_000)      # the real Bob, with the real password
            self.assertTrue(second.service.ledger.reconcile()["ok"])


class TestFrontEndHygiene(unittest.TestCase):
    def test_no_dangerous_dom_apis_in_the_script(self):
        js = (STATIC / "app.js").read_text()
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "localStorage", "sessionStorage"):
            self.assertNotIn(banned, js, banned)

    def test_html_has_no_inline_code_and_no_external_resources(self):
        html = (STATIC / "index.html").read_text()
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html), [])
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
        self.assertIsNone(re.search(r"\sstyle\s*=", html))
        self.assertIsNone(re.search(r"(src|href)=[\"']https?://", html))
        self.assertNotIn("style=", (STATIC / "app.js").read_text())          # blocked by the CSP anyway

    def test_recovery_codes_are_gone_from_the_interface(self):
        for name in ("index.html", "app.js"):
            self.assertNotIn("recovery", (STATIC / name).read_text().lower(), name)

    def test_every_money_action_asks_for_the_pin(self):
        js = (STATIC / "app.js").read_text()
        for needle in ('"/api/wallet/deposits", await withPin', '"/api/withdrawals", await withPin', '"/api/positions", await withPin',
                       '"/execute", await withPin', '"/api/payouts/accounts", await withPin', '"/api/live/deposits", await withPin',
                       '"Open these positions")', '"Close this position")', '"Remove this payout account")'):
            self.assertIn(needle, js, needle)

    def test_resource_ids_are_not_percent_encoded_in_paths(self):
        # Ids use only [A-Za-z0-9_.:-]; the strict router does not decode %3A, so encoding them would 404.
        js = (STATIC / "app.js").read_text()
        for name in ("w.withdrawal_id", "q.quote_id", "p.position_id"):
            self.assertNotIn(f"encodeURIComponent({name})", js)

    def test_every_element_the_script_looks_up_exists(self):
        html, js = (STATIC / "index.html").read_text(), (STATIC / "app.js").read_text()
        for ident in set(re.findall(r'\$\("#([\w-]+)"\)', js)):
            self.assertIn(f'id="{ident}"', html, ident)
        for form in set(re.findall(r"^\s{2}(?:async )?(\w+)\(f\)", js, re.M)):
            self.assertIn(f'data-form="{form}"', html, form)


if __name__ == "__main__":
    unittest.main()
