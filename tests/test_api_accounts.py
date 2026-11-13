import time
import unittest

from app.accounts import MemoryMailer, totp
from app.payouts import BankDirectory, nuban
try:
    from tests.api_client import Client, PASSWORD, RunningServer
except ImportError:
    from api_client import Client, PASSWORD, RunningServer

GT_ACCOUNT = "012345678" + str(nuban.check_digit("058", "012345678"))       # a valid GTBank account number
NEW_PASSWORD = "a-brand-new-password"


class ForcedNames:
    """Name enquiry that recognises the account at every bank: the worst, ambiguous case."""

    def lookup(self, bank_code, account_number):
        return f"NAME AT {bank_code}"


def dest_body(**extra):
    return {"currency": "NGN", "amount_minor": 4_000_000, "destination": {"account_number": GT_ACCOUNT}, **extra}


class ApiCase(unittest.TestCase):
    server_args: dict = {}

    def setUp(self):
        self.server = RunningServer(**self.server_args)
        self.addCleanup(self.server.stop)
        self.mailer = MemoryMailer()
        self.server.service.accounts.mailer = self.mailer
        self.alice = Client(self.server).register_login("alice")
        self.alice.post("/api/wallet/deposits", {"currency": "NGN", "amount_minor": 300_000_000}, key=True)


class TestBankCodeIsOptional(ApiCase):
    def test_withdraw_with_only_an_account_number(self):
        status, w, _ = self.alice.post("/api/withdrawals", dest_body(), key=True)
        self.assertEqual(status, 201, w)
        self.assertEqual(w["destination"]["account_number"], GT_ACCOUNT)
        self.assertTrue(nuban.validates(w["destination"]["bank_code"], GT_ACCOUNT))
        self.assertTrue(w["bank_name"])

    def test_resolve_then_save_then_withdraw_to_the_saved_account(self):
        status, found, _ = self.alice.post("/api/payouts/resolve", {"account_number": GT_ACCOUNT})
        self.assertEqual(status, 200)
        named = [c for c in found["candidates"] if c["account_name"]]
        self.assertEqual(len(named), 1)
        self.assertTrue(found["simulated"])
        status, saved, _ = self.alice.post("/api/payouts/accounts", {"currency": "NGN", "label": "My bank", "destination": {"account_number": GT_ACCOUNT}})
        self.assertEqual(status, 200, saved)
        self.assertEqual(len(self.alice.get("/api/payouts/accounts")[1]["accounts"]), 1)
        status, w, _ = self.alice.post("/api/withdrawals", {"currency": "NGN", "amount_minor": 4_000_000, "beneficiary_id": saved["beneficiary_id"]}, key=True)
        self.assertEqual(status, 201, w)
        self.assertEqual(w["destination"]["account_number"], GT_ACCOUNT)
        self.assertEqual(self.alice.post(f"/api/payouts/accounts/{saved['beneficiary_id']}/delete")[0], 200)
        self.assertEqual(self.alice.get("/api/payouts/accounts")[1]["accounts"], [])

    def test_a_wrong_bank_for_the_account_number_is_rejected(self):
        wrong = next(b.code for b in BankDirectory.load().banks if not nuban.validates(b.code, GT_ACCOUNT))
        body = dest_body()
        body["destination"]["bank_code"] = wrong
        status, error, _ = self.alice.post("/api/withdrawals", body, key=True)
        self.assertEqual(status, 400)
        self.assertIn("does not match", error["error"]["message"])

    def test_exactly_one_of_destination_or_beneficiary(self):
        both = dest_body(beneficiary_id="b_x")
        neither = {"currency": "NGN", "amount_minor": 4_000_000}
        self.assertEqual(self.alice.post("/api/withdrawals", both, key=True)[0], 400)
        self.assertEqual(self.alice.post("/api/withdrawals", neither, key=True)[0], 400)

    def test_payout_accounts_are_private(self):
        saved = self.alice.post("/api/payouts/accounts", {"currency": "NGN", "label": "Mine", "destination": {"account_number": GT_ACCOUNT}})[1]
        mallory = Client(self.server).register_login("mallory").verify_kyc()
        mallory.post("/api/wallet/deposits", {"currency": "NGN", "amount_minor": 100_000_000}, key=True)
        self.assertEqual(mallory.get("/api/payouts/accounts")[1]["accounts"], [])
        self.assertEqual(mallory.post(f"/api/payouts/accounts/{saved['beneficiary_id']}/delete")[0], 404)
        body = {"currency": "NGN", "amount_minor": 4_000_000, "beneficiary_id": saved["beneficiary_id"]}
        self.assertEqual(mallory.post("/api/withdrawals", body, key=True)[0], 404)

    def test_currency_must_match_the_saved_account(self):
        saved = self.alice.post("/api/payouts/accounts", {"currency": "NGN", "label": "Mine", "destination": {"account_number": GT_ACCOUNT}})[1]
        body = {"currency": "USDT", "amount_minor": 10_000_000, "beneficiary_id": saved["beneficiary_id"]}
        self.assertEqual(self.alice.post("/api/withdrawals", body, key=True)[0], 400)

    def test_payout_endpoints_validate_input(self):
        for body in ({"account_number": "123"}, {"account_number": 1234567890}, {}, {"account_number": GT_ACCOUNT, "x": 1}):
            self.assertEqual(self.alice.post("/api/payouts/resolve", body)[0], 400, body)
        for label in ("", "<script>alert(1)</script>", "x" * 50):
            self.assertEqual(self.alice.post("/api/payouts/accounts", {"currency": "NGN", "label": label, "destination": {"account_number": GT_ACCOUNT}})[0], 400)


class TestAmbiguousBank(unittest.TestCase):
    def test_customer_chooses_when_several_banks_claim_the_account(self):
        server = RunningServer(service_kwargs={"name_enquiry": ForcedNames()})
        self.addCleanup(server.stop)
        alice = Client(server).register_login("alice").verify_kyc()
        alice.post("/api/wallet/deposits", {"currency": "NGN", "amount_minor": 100_000_000}, key=True)
        status, error, _ = alice.post("/api/withdrawals", dest_body(), key=True)
        self.assertEqual((status, error["error"]["code"]), (409, "ambiguous_bank"))
        candidates = error["error"]["candidates"]
        self.assertGreaterEqual(len(candidates), 2)
        body = dest_body()
        body["destination"]["bank_code"] = candidates[0]["bank_code"]            # the customer picks by bank name
        self.assertEqual(alice.post("/api/withdrawals", body, key=True)[0], 201)


class TestKycLimits(ApiCase):
    def test_unverified_users_have_small_limits_and_kyc_raises_them(self):
        big = dest_body(amount_minor=40_000_000)                                  # NGN 400,000
        status, error, _ = self.alice.post("/api/withdrawals", big, key=True)
        self.assertEqual(status, 422)
        self.assertEqual(self.alice.get("/api/security")[1]["kyc"]["tier"], 0)
        self.assertEqual(self.alice.post("/api/kyc/basic", {"full_name": "Ada Obi", "bvn": "12345678901"})[1]["tier"], 1)
        self.assertEqual(self.alice.post("/api/withdrawals", big, key=True)[0], 201)

    def test_bvn_cannot_be_reused_and_is_never_echoed(self):
        status, body, _ = self.alice.post("/api/kyc/basic", {"full_name": "Ada Obi", "bvn": "12345678901"})
        self.assertNotIn("12345678901", str(body))
        other = Client(self.server).register_login("bobby")
        self.assertEqual(other.post("/api/kyc/basic", {"full_name": "Chidi Eze", "bvn": "12345678901"})[0], 409)
        self.assertEqual(other.post("/api/kyc/basic", {"full_name": "Chidi Eze", "bvn": "123"})[0], 400)
        self.assertEqual(other.post("/api/kyc/enhanced", {"address": "12 Marina Street, Lagos"})[0], 400)    # tier 1 first

    def test_kyc_is_not_auto_approved_outside_simulation(self):
        server = RunningServer(simulation_mode=False)
        self.addCleanup(server.stop)
        c = Client(server).register_login("bobby")
        self.assertEqual(c.post("/api/kyc/basic", {"full_name": "Chidi Eze", "bvn": "12345678902"})[1]["status"], "pending")
        self.assertEqual(c.get("/api/security")[1]["kyc"]["tier"], 0)


class TestAuthenticatorOverHttp(ApiCase):
    def enable(self, client):
        secret = client.post("/api/security/totp/begin", {"password": PASSWORD})[1]["secret"]
        status, body, _ = client.post("/api/security/totp/confirm", {"code": totp.code_at(secret, totp.current_step(time.time()))})
        self.assertEqual(status, 200, body)
        self.assertNotIn("recovery", str(body).lower())
        self.server.service.accounts.clock = lambda: time.time() + 31          # the next 30-second step
        return secret

    def code(self, secret):
        return totp.code_at(secret, totp.current_step(time.time() + 31))

    def test_login_needs_the_authenticator_code_when_enabled(self):
        secret = self.enable(self.alice)
        guest = Client(self.server)
        status, body, _ = guest.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD})
        self.assertEqual((status, body["error"]["code"]), (401, "otp_required"))
        status, body, _ = guest.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD, "otp": "000000"})
        self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
        status, body, headers = guest.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD, "otp": self.code(secret)})
        self.assertEqual(status, 200, body)
        self.assertIn("Set-Cookie", headers)

    def test_recovery_codes_no_longer_exist_anywhere(self):
        self.enable(self.alice)
        security = self.alice.get("/api/security")[1]
        self.assertNotIn("recovery_codes_left", security["totp"])
        guest = Client(self.server)
        status, body, _ = guest.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD, "otp": "abcde-12345"})
        self.assertEqual(status, 401)
        self.assertIsNone(self.server.service.ledger.db.execute("SELECT name FROM sqlite_master WHERE name = 'recovery_codes'").fetchone())

    def test_withdrawals_use_the_pin_not_the_authenticator(self):
        self.enable(self.alice.verify_kyc())
        self.assertEqual(self.alice.post("/api/withdrawals", dest_body(), key=True)[0], 201)       # PIN attached by the client

    def test_disabling_needs_password_and_a_current_code_and_csrf(self):
        secret = self.enable(self.alice)
        body = {"password": PASSWORD, "code": self.code(secret)}
        self.assertEqual(self.alice.request("POST", "/api/security/totp/disable", body, csrf=False)[0], 403)
        self.assertEqual(self.alice.post("/api/security/totp/disable", {"password": "wrong-password-1", "code": body["code"]})[0], 401)
        self.assertEqual(self.alice.post("/api/security/totp/disable", body)[0], 200)
        self.assertFalse(self.alice.get("/api/security")[1]["totp"]["enabled"])

    def test_cannot_enrol_twice_or_confirm_without_starting(self):
        c = Client(self.server).register_login("bobby")
        self.assertEqual(c.post("/api/security/totp/confirm", {"code": "123456"})[0], 409)
        self.enable(c)
        self.assertEqual(c.post("/api/security/totp/begin", {"password": PASSWORD})[0], 409)

    def test_email_change_requires_password_and_current_totp_when_enabled(self):
        secret = self.enable(self.alice)
        self.assertEqual(self.alice.post("/api/security/email", {"email": "attacker@example.com"})[0], 400)
        self.assertEqual(self.alice.post("/api/security/email", {"email": "attacker@example.com", "password": "wrong-password-1"})[0], 401)
        self.assertEqual(self.alice.post("/api/security/email", {"email": "attacker@example.com", "password": PASSWORD,
                                                                  "otp": "000000"})[0], 401)
        status, body, _ = self.alice.post("/api/security/email", {"email": "alice-new@example.com", "password": PASSWORD,
                                                                    "otp": self.code(secret)})
        self.assertEqual(status, 200, body)
        security = self.alice.get("/api/security")[1]
        self.assertIsNone(security["email"])
        self.assertEqual(security["pending_email"], "alice-new@example.com")
        pending_code = self.mailer.sent[-1][2].split("code is ")[1][:8]
        self.assertEqual(self.alice.post("/api/security/email/verify", {"code": pending_code})[0], 200)
        self.assertEqual(self.alice.get("/api/security")[1]["email"], "alice-new@example.com")

    def test_totp_enrollment_requires_current_password(self):
        self.assertEqual(self.alice.post("/api/security/totp/begin", {})[0], 400)
        self.assertEqual(self.alice.post("/api/security/totp/begin", {"password": "wrong-password-1"})[0], 401)
        self.assertEqual(self.alice.post("/api/security/totp/begin", {"password": PASSWORD})[0], 200)

    def test_a_lost_phone_is_recovered_by_password_reset(self):
        secret = self.enable(self.alice)
        self.alice.post("/api/security/email", {"email": "alice@example.com", "password": PASSWORD,
                                                 "otp": self.code(secret)})
        self.alice.post("/api/security/email/verify", {"code": self.mailer.sent[-1][2].split("code is ")[1][:8]})
        guest = Client(self.server)
        guest.request("POST", "/api/auth/password-reset/request", {"email": "alice@example.com"})
        reset = {"email": "alice@example.com", "code": self.mailer.sent[-1][2].split("code is ")[1][:8], "new_password": NEW_PASSWORD}
        self.assertEqual(guest.request("POST", "/api/auth/password-reset/confirm", reset)[0], 200)
        self.assertEqual(guest.request("POST", "/api/auth/login", {"username": "alice", "password": NEW_PASSWORD})[0], 200)


class TestTransactionPinOverHttp(ApiCase):
    def test_nothing_moves_until_a_pin_is_set_then_every_action_needs_it(self):
        bob = Client(self.server)
        bob.request("POST", "/api/auth/register", {"username": "bobby", "password": PASSWORD})
        _, body, headers = bob.request("POST", "/api/auth/login", {"username": "bobby", "password": PASSWORD})
        bob.cookie, bob.csrf = headers["Set-Cookie"].split(";")[0].split("=", 1)[1], body["csrf_token"]
        self.assertFalse(bob.get("/api/security")[1]["pin"]["set"])
        status, error, _ = bob.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 1_000_000, "pin": "482915"}, key=True)
        self.assertEqual((status, error["error"]["code"]), (403, "pin_not_set"))
        self.assertEqual(bob.post("/api/security/pin/set", {"password": "wrong-password-1", "pin": "482915"})[0], 401)
        self.assertEqual(bob.post("/api/security/pin/set", {"password": PASSWORD, "pin": "123456"})[0], 400)        # too easy
        self.assertEqual(bob.post("/api/security/pin/set", {"password": PASSWORD, "pin": "482915"})[0], 200)
        self.assertEqual(bob.post("/api/security/pin/set", {"password": PASSWORD, "pin": "593017"})[0], 409)
        body = {"currency": "USDT", "amount_minor": 1_000_000}
        status, error, _ = bob.request("POST", "/api/wallet/deposits", body, key=True, pin=False)
        self.assertEqual((status, error["error"]["code"]), (403, "pin_required"))
        status, error, _ = bob.request("POST", "/api/wallet/deposits", {**body, "pin": "000000"}, key=True, pin=False)
        self.assertEqual((status, error["error"]["code"]), (403, "invalid_pin"))
        self.assertEqual(bob.request("POST", "/api/wallet/deposits", {**body, "pin": "482915"}, key=True, pin=False)[0], 200)
        self.assertEqual(bob.get("/api/wallet")[1]["balances"]["USDT"], 1_000_000)

    def test_the_pin_is_never_echoed_or_stored_in_the_audit_trail(self):
        self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 1_000_000}, key=True)
        dump = str(self.alice.get("/api/activity")[1]) + str(self.alice.get("/api/security")[1])
        self.assertNotIn("482915", dump)

    def test_five_wrong_pins_return_429(self):
        for _ in range(5):
            self.assertEqual(self.alice.request("POST", "/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100, "pin": "000000"},
                                                key=True, pin=False)[0], 403)
        status, _, headers = self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100}, key=True)
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)

    def test_replays_work_without_the_pin(self):
        body = {"currency": "USDT", "amount_minor": 1_000_000}
        first = self.alice.post("/api/wallet/deposits", body, key="replay-key-0001")
        again = self.alice.request("POST", "/api/wallet/deposits", body, key="replay-key-0001", pin=False)
        self.assertEqual((again[0], again[1]), (first[0], first[1]))

    def test_change_and_forgot_pin(self):
        self.assertEqual(self.alice.post("/api/security/pin/change", {"current_pin": "000000", "new_pin": "593017"})[0], 403)
        self.assertEqual(self.alice.post("/api/security/pin/change", {"current_pin": "482915", "new_pin": "593017"})[0], 200)
        self.alice.pin = "593017"
        self.assertEqual(self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100}, key=True)[0], 200)
        self.assertEqual(self.alice.post("/api/security/pin/reset/request")[0], 400)                 # email not verified
        self.alice.post("/api/security/email", {"email": "alice@example.com", "password": PASSWORD})
        self.alice.post("/api/security/email/verify", {"code": self.mailer.sent[-1][2].split("code is ")[1][:8]})
        self.assertEqual(self.alice.post("/api/security/pin/reset/request")[0], 200)
        code = self.mailer.sent[-1][2].split("code is ")[1][:8]
        self.assertEqual(self.alice.post("/api/security/pin/reset/confirm", {"code": code, "password": PASSWORD, "new_pin": "671234"})[0], 200)
        self.alice.pin = "671234"
        self.assertEqual(self.alice.post("/api/wallet/deposits", {"currency": "USDT", "amount_minor": 100}, key=True)[0], 200)

    def test_payout_accounts_need_the_pin(self):
        body = {"currency": "NGN", "label": "Mine", "destination": {"account_number": GT_ACCOUNT}}
        status, error, _ = self.alice.request("POST", "/api/payouts/accounts", body, pin=False)
        self.assertEqual((status, error["error"]["code"]), (403, "pin_required"))
        self.assertEqual(self.alice.post("/api/payouts/accounts", body)[0], 200)


class TestEmailAndReset(ApiCase):
    def code(self):
        return self.mailer.sent[-1][2].split("code is ")[1][:8]

    def test_verify_email_then_reset_password(self):
        self.assertEqual(self.alice.post("/api/security/email", {"email": "alice@example.com", "password": PASSWORD})[0], 200)
        self.assertEqual(self.alice.post("/api/security/email/verify", {"code": "00000000"})[0], 401)
        self.assertEqual(self.alice.post("/api/security/email/verify", {"code": self.code()})[0], 200)
        self.assertTrue(self.alice.get("/api/security")[1]["email_verified"])
        guest = Client(self.server)
        known = guest.request("POST", "/api/auth/password-reset/request", {"email": "alice@example.com"})
        unknown = guest.request("POST", "/api/auth/password-reset/request", {"email": "nobody@example.com"})
        self.assertEqual((known[0], known[1]), (unknown[0], unknown[1]))                       # indistinguishable
        reset = {"email": "alice@example.com", "code": self.code(), "new_password": NEW_PASSWORD}
        self.assertEqual(guest.request("POST", "/api/auth/password-reset/confirm", reset)[0], 200)
        self.assertEqual(self.alice.get("/api/wallet")[0], 401)                                  # old session revoked
        self.assertEqual(guest.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD})[0], 401)
        self.assertEqual(guest.request("POST", "/api/auth/login", {"username": "alice", "password": NEW_PASSWORD})[0], 200)

    def test_email_change_keeps_old_verified_address_until_new_one_is_verified(self):
        # Establish the original recovery address.
        status, body, _ = self.alice.post(
            "/api/security/email", {"email": "alice@example.com", "password": PASSWORD}
        )
        self.assertEqual(status, 200, body)
        original_code = self.code()
        self.assertEqual(self.alice.post("/api/security/email/verify", {"code": original_code})[0], 200)

        # Request a change: the original verified address remains active.
        status, body, _ = self.alice.post(
            "/api/security/email", {"email": "alice-new@example.com", "password": PASSWORD}
        )
        self.assertEqual(status, 200, body)
        status, security, _ = self.alice.get("/api/security")
        self.assertEqual(status, 200)
        self.assertEqual(security["email"], "alice@example.com")
        self.assertTrue(security["email_verified"])
        self.assertEqual(security["pending_email"], "alice-new@example.com")
        self.assertEqual(self.mailer.sent[-1][0], "alice-new@example.com")
        pending_code = self.code()

        # The old address remains usable for password recovery while the change is pending.
        guest = Client(self.server)
        old_request = guest.request(
            "POST", "/api/auth/password-reset/request", {"email": "alice@example.com"}
        )
        new_request = guest.request(
            "POST", "/api/auth/password-reset/request", {"email": "alice-new@example.com"}
        )
        self.assertEqual(old_request[0], 200)
        self.assertEqual(new_request[0], 200)
        self.assertNotEqual(self.mailer.sent[-1][0], "alice-new@example.com")

        # The new address becomes the recovery address only after its verification code succeeds.
        self.assertEqual(
            self.alice.post("/api/security/email/verify", {"code": pending_code})[0], 200
        )
        status, security, _ = self.alice.get("/api/security")
        self.assertEqual(status, 200)
        self.assertEqual(security["email"], "alice-new@example.com")
        self.assertTrue(security["email_verified"])
        self.assertIsNone(security["pending_email"])

    def test_weak_password_does_not_consume_valid_reset_code(self):
        guest = Client(self.server)
        self.alice.post(
            "/api/security/email",
            {"email": "alice@example.com", "password": PASSWORD},
        )
        self.assertEqual(
            self.alice.post(
                "/api/security/email/verify", {"code": self.code()}
            )[0],
            200,
        )
        guest.request(
            "POST",
            "/api/auth/password-reset/request",
            {"email": "alice@example.com"},
        )

        reset_code = self.code()
        reset = {
            "email": "alice@example.com",
            "code": reset_code,
            "new_password": "weak",
        }

        weak_response = guest.request(
            "POST", "/api/auth/password-reset/confirm", reset
        )
        self.assertEqual(weak_response[0], 400)

        valid_reset = {**reset, "new_password": NEW_PASSWORD}
        successful_response = guest.request(
            "POST", "/api/auth/password-reset/confirm", valid_reset
        )
        self.assertEqual(successful_response[0], 200)

    def test_reset_with_a_wrong_code_or_unverified_email_fails_uniformly(self):
        guest = Client(self.server)
        self.alice.post("/api/security/email", {"email": "alice@example.com", "password": PASSWORD})
        body = {"email": "alice@example.com", "code": self.code(), "new_password": NEW_PASSWORD}
        unverified = guest.request("POST", "/api/auth/password-reset/confirm", body)
        nobody = guest.request("POST", "/api/auth/password-reset/confirm", {**body, "email": "nobody@example.com"})
        self.assertEqual((unverified[0], unverified[1]), (nobody[0], nobody[1]))
        self.assertEqual(unverified[0], 400)

    def test_reset_password_errors_do_not_reveal_verified_email_existence(self):
        guest = Client(self.server)

        self.alice.post(
            "/api/security/email",
            {"email": "alice@example.com", "password": PASSWORD},
        )
        self.assertEqual(
            self.alice.post(
                "/api/security/email/verify", {"code": self.code()}
            )[0],
            200,
        )

        known = guest.request(
            "POST",
            "/api/auth/password-reset/confirm",
            {
                "email": "alice@example.com",
                "code": "00000000",
                "new_password": "weak",
            },
        )
        unknown = guest.request(
            "POST",
            "/api/auth/password-reset/confirm",
            {
                "email": "nobody@example.com",
                "code": "00000000",
                "new_password": "weak",
            },
        )

        self.assertEqual((known[0], known[1]), (unknown[0], unknown[1]))
        self.assertEqual(known[0], 400)
        self.assertEqual(known[1]["error"]["code"], "invalid_reset")

    def test_registration_accepts_an_optional_email(self):
        c = Client(self.server)
        self.assertEqual(c.request("POST", "/api/auth/register", {"username": "bobby", "password": PASSWORD, "email": "bobby@example.com"})[0], 201)
        self.assertEqual(self.mailer.sent[-1][0], "bobby@example.com")
        self.assertEqual(c.request("POST", "/api/auth/register", {"username": "carol", "password": PASSWORD, "email": "not-an-email"})[0], 400)


class TestBehindATlsProxy(unittest.TestCase):
    PUBLIC = "lab.example.com"

    def server(self, **kw):
        server = RunningServer(public_host=self.PUBLIC, public_https=True, **kw)
        self.addCleanup(server.stop)
        return server

    def test_public_host_origin_cookie_and_hsts(self):
        server = self.server()
        c = Client(server)
        proxied = {"Host": self.PUBLIC, "Origin": f"https://{self.PUBLIC}"}
        self.assertEqual(c.request("POST", "/api/auth/register", {"username": "alice", "password": PASSWORD}, headers=proxied)[0], 201)
        status, body, headers = c.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD}, headers=proxied)
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("max-age=31536000", headers["Strict-Transport-Security"])
        self.assertEqual(c.request("POST", "/api/auth/login", {"username": "alice", "password": PASSWORD},
                                   headers={"Host": self.PUBLIC, "Origin": f"http://{self.PUBLIC}"})[0], 403)      # downgrade refused
        self.assertEqual(c.get("/api/health", headers={"Host": "evil.example"})[0], 400)

    def test_forwarded_for_is_only_trusted_when_configured(self):
        for trusted, expect_limited in ((False, True), (True, False)):
            server = self.server(trust_proxy=trusted)
            server.server.api.ip_limiter = __import__("app.accounts", fromlist=["RateLimiter"]).RateLimiter(3, 60)
            statuses = [Client(server).get("/api/health", headers={"X-Forwarded-For": f"203.0.113.{i}"})[0] for i in range(6)]
            self.assertEqual(429 in statuses, expect_limited, trusted)

    def test_bad_forwarded_for_falls_back_to_the_peer(self):
        server = self.server(trust_proxy=True)
        statuses = [Client(server).get("/api/health", headers={"X-Forwarded-For": "not-an-ip"})[0] for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 200])

    def test_public_host_must_be_a_plain_host_name(self):
        from app.config import Settings
        for bad in ("evil.com/path", "a b", "http://x.com", "x.com:99999999"):
            with self.assertRaises(ValueError):
                Settings(public_host=bad)


if __name__ == "__main__":
    unittest.main()
