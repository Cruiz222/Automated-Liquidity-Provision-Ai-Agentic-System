import base64
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.accounts import (
    AccountStore, AuthError, ConflictError, ConsoleMailer, InvalidPin, KycConflict, KycError, KycStore, MemoryMailer,
    OtpRequired, PinNotSet, PinRequired, RateLimited, SmtpMailer, ValidationError, totp, validate_pin,
)
from app.ledger import WalletLedger
from app.service import load_secret

PW = "correct-horse-battery"
NEW_PW = "a-completely-new-password"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Case(unittest.TestCase):
    def setUp(self):
        self.clock, self.mailer, self.ledger = Clock(), MemoryMailer(), WalletLedger()
        self.addCleanup(self.ledger.close)
        self.accounts = AccountStore(self.ledger, password_iterations=1000, clock=self.clock, limiter_clock=self.clock,
                                     mailer=self.mailer, pin_pepper=b"p" * 32)

    def last_code(self):
        return self.mailer.sent[-1][2].split("code is ")[1][:8]

    def code_now(self, secret):
        return totp.code_at(secret, totp.current_step(self.clock.now))

    def enable_2fa(self, user_id):
        secret = self.accounts.totp_begin(user_id, PW)["secret"]
        self.accounts.totp_confirm(user_id, self.code_now(secret))
        return secret, None


class TestTotp(unittest.TestCase):
    SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")

    def test_rfc_6238_vectors(self):
        self.assertEqual(totp.code_at(self.SECRET, 59 // 30, 8), "94287082")
        self.assertEqual(totp.code_at(self.SECRET, 1111111109 // 30, 8), "07081804")
        self.assertEqual(totp.code_at(self.SECRET, 2000000000 // 30, 8), "69279037")

    def test_window_replay_and_input_checks(self):
        now = 1_111_111_109
        step = totp.current_step(now)
        code = totp.code_at(self.SECRET, step)
        self.assertEqual(totp.verify(self.SECRET, code, now), step)
        self.assertEqual(totp.verify(self.SECRET, totp.code_at(self.SECRET, step - 1), now), step - 1)   # one step of clock drift
        self.assertIsNone(totp.verify(self.SECRET, totp.code_at(self.SECRET, step - 2), now))
        self.assertIsNone(totp.verify(self.SECRET, code, now, last_step=step))                             # no reuse
        for bad in ("", "12345", "1234567", "abcdef", None, 123456):
            self.assertIsNone(totp.verify(self.SECRET, bad, now))

    def test_uri_and_secret(self):
        secret = totp.generate_secret()
        self.assertEqual(len(secret), 32)
        self.assertIn("secret=" + secret, totp.otpauth_uri(secret, "alice", "Lab"))


class TestTwoFactor(Case):
    def setUp(self):
        super().setUp()
        self.user = self.accounts.register("alice", PW)["user_id"]

    def test_login_requires_a_code_once_enabled(self):
        secret, _ = self.enable_2fa(self.user)
        with self.assertRaises(OtpRequired):
            self.accounts.login("alice", PW)
        self.clock.now += 30
        self.assertTrue(self.accounts.login("alice", PW, otp=self.code_now(secret))["token"])

    def test_wrong_codes_count_toward_lockout_and_do_not_reveal_which_factor(self):
        self.enable_2fa(self.user)
        for _ in range(5):
            with self.assertRaises(AuthError) as ctx:
                self.accounts.login("alice", PW, otp="000000")
            self.assertNotIsInstance(ctx.exception, OtpRequired)
        with self.assertRaises(RateLimited):
            self.accounts.login("alice", PW, otp="000000")

    def test_a_code_cannot_be_used_twice(self):
        secret, _ = self.enable_2fa(self.user)
        self.clock.now += 30
        code = self.code_now(secret)
        self.accounts.login("alice", PW, otp=code)
        with self.assertRaises(AuthError):
            self.accounts.login("alice", PW, otp=code)
        self.clock.now += 30
        self.assertTrue(self.accounts.login("alice", PW, otp=self.code_now(secret)))

    def test_enrolment_needs_a_valid_code_and_cannot_be_repeated(self):
        secret = self.accounts.totp_begin(self.user, PW)["secret"]
        with self.assertRaises(AuthError):
            self.accounts.totp_confirm(self.user, "000000")
        self.assertFalse(self.accounts.totp_status(self.user)["enabled"])
        self.accounts.totp_confirm(self.user, self.code_now(secret))
        with self.assertRaises(ConflictError):
            self.accounts.totp_begin(self.user, PW)
        with self.assertRaises(ConflictError):
            self.accounts.totp_confirm(self.user, self.code_now(secret))

    def test_disable_needs_password_and_code(self):
        secret, _ = self.enable_2fa(self.user)
        self.clock.now += 30
        with self.assertRaises(AuthError):
            self.accounts.totp_disable(self.user, "wrong-password-1", self.code_now(secret))
        self.accounts.totp_disable(self.user, PW, self.code_now(secret))
        self.assertTrue(self.accounts.login("alice", PW))
        self.assertIsNone(self.ledger.db.execute("SELECT name FROM sqlite_master WHERE name = 'recovery_codes'").fetchone())


class TestTransactionPin(Case):
    PIN = "482915"

    def setUp(self):
        super().setUp()
        self.user = self.accounts.register("alice", PW, email="a@example.com")["user_id"]

    def test_pin_rules(self):
        for bad in ("12345", "1234567", "abcdef", "", None, 482915, "111111", "123456", "654321", "121212", "000000", "123123"):
            with self.assertRaises(ValidationError, msg=str(bad)):
                validate_pin(bad)
        self.assertEqual(validate_pin(self.PIN), self.PIN)

    def test_no_pin_means_no_transactions(self):
        with self.assertRaises(PinNotSet):
            self.accounts.verify_pin(self.user, self.PIN)
        self.assertEqual(self.accounts.pin_status(self.user), {"set": False})

    def test_setting_the_pin_needs_the_password_and_works_once(self):
        with self.assertRaises(AuthError):
            self.accounts.set_pin(self.user, "wrong-password-1", self.PIN)
        self.accounts.set_pin(self.user, PW, self.PIN)
        self.accounts.verify_pin(self.user, self.PIN)
        with self.assertRaises(ConflictError):
            self.accounts.set_pin(self.user, PW, "593017")                      # changing needs the old PIN or an email reset

    def test_the_pin_is_hashed_and_peppered(self):
        self.accounts.set_pin(self.user, PW, self.PIN)
        stored = self.ledger.db.execute("SELECT pin_hash FROM users").fetchone()[0]
        self.assertTrue(stored.startswith("pin1$"))
        self.assertNotIn(self.PIN, stored)
        other = AccountStore(self.ledger, password_iterations=1000, clock=self.clock, limiter_clock=self.clock,
                             mailer=self.mailer, pin_pepper=b"q" * 32)         # same database, different server secret
        with self.assertRaises(InvalidPin):
            other.verify_pin(self.user, self.PIN)

    def test_wrong_and_missing_pins(self):
        self.accounts.set_pin(self.user, PW, self.PIN)
        with self.assertRaises(PinRequired):
            self.accounts.verify_pin(self.user, None)
        for bad in ("000000", "48291", "abcdef", 482915):
            with self.assertRaises(InvalidPin):
                self.accounts.verify_pin(self.user, bad)

    def test_five_wrong_pins_lock_pin_use(self):
        self.accounts.set_pin(self.user, PW, self.PIN)
        for _ in range(5):
            with self.assertRaises(InvalidPin):
                self.accounts.verify_pin(self.user, "000000")
        with self.assertRaises(RateLimited):
            self.accounts.verify_pin(self.user, self.PIN)                       # even the right PIN waits
        self.clock.now += 901
        self.accounts.verify_pin(self.user, self.PIN)

    def test_change_pin(self):
        self.accounts.set_pin(self.user, PW, self.PIN)
        with self.assertRaises(InvalidPin):
            self.accounts.change_pin(self.user, "000000", "593017")
        with self.assertRaises(ValidationError):
            self.accounts.change_pin(self.user, self.PIN, self.PIN)
        self.accounts.change_pin(self.user, self.PIN, "593017")
        self.accounts.verify_pin(self.user, "593017")
        with self.assertRaises(InvalidPin):
            self.accounts.verify_pin(self.user, self.PIN)

    def test_forgotten_pin_is_reset_by_email(self):
        self.accounts.set_pin(self.user, PW, self.PIN)
        with self.assertRaises(ValidationError):
            self.accounts.request_pin_reset(self.user)                          # email not verified yet
        self.accounts.verify_email(self.user, self.last_code())
        self.accounts.request_pin_reset(self.user)
        code = self.last_code()
        with self.assertRaises(AuthError):
            self.accounts.reset_pin(self.user, "00000000", PW, "593017")
        with self.assertRaises(AuthError):
            self.accounts.reset_pin(self.user, code, "wrong-password-1", "593017")
        self.accounts.reset_pin(self.user, code, PW, "593017")
        self.accounts.verify_pin(self.user, "593017")
        with self.assertRaises(AuthError):
            self.accounts.reset_pin(self.user, code, PW, "671234")              # the code was single-use

    def test_reset_clears_a_pin_lockout(self):
        self.accounts.set_pin(self.user, PW, self.PIN)
        self.accounts.verify_email(self.user, self.last_code())
        for _ in range(5):
            with self.assertRaises(InvalidPin):
                self.accounts.verify_pin(self.user, "000000")
        self.accounts.request_pin_reset(self.user)
        self.accounts.reset_pin(self.user, self.last_code(), PW, "593017")
        self.accounts.verify_pin(self.user, "593017")


class TestEmail(Case):
    def test_email_code_digest_is_keyed_and_not_reusable_with_another_server_secret(self):
        digest = self.accounts._hash_code("01234567", "u_test", "verify")
        other = AccountStore(self.ledger, password_iterations=1000, clock=self.clock, limiter_clock=self.clock,
                             mailer=self.mailer, pin_pepper=b"r" * 32)
        self.assertNotEqual(digest, other._hash_code("01234567", "u_test", "verify"))
        self.assertEqual(len(digest), 64)

    def test_failed_email_delivery_retires_code_and_allows_resend(self):
        user = self.accounts.register(
            "alice", PW, email="original@example.com"
        )["user_id"]
        self.accounts.verify_email(user, self.last_code())

        failed_mailer = mock.Mock()
        failed_mailer.send.side_effect = OSError("SMTP unavailable")
        self.accounts.mailer = failed_mailer

        with self.assertRaisesRegex(OSError, "SMTP unavailable"):
            self.accounts.set_email(user, "new@example.com", PW)

        status = self.accounts.security_status(user)
        self.assertEqual(status["email"], "original@example.com")
        self.assertTrue(status["email_verified"])
        self.assertEqual(status["pending_email"], "new@example.com")

        active_codes = self.ledger.db.execute(
            "SELECT COUNT(*) FROM email_codes "
            "WHERE user_id = ? AND purpose = 'verify' AND used = 0",
            (user,),
        ).fetchone()[0]

        self.assertEqual(active_codes, 0)

        # A fresh delivery can complete the pending email change.
        self.accounts.mailer = self.mailer
        self.accounts.send_email_code(user, "verify")
        self.accounts.verify_email(user, self.last_code())

        status = self.accounts.security_status(user)
        self.assertEqual(status["email"], "new@example.com")
        self.assertTrue(status["email_verified"])
        self.assertIsNone(status["pending_email"])

    def test_cleanup_failure_does_not_mask_email_delivery_error(self):
        user = self.accounts.register(
            "alice", PW, email="alice@example.com"
        )["user_id"]

        failed_mailer = mock.Mock()
        failed_mailer.send.side_effect = OSError("SMTP unavailable")
        self.accounts.mailer = failed_mailer

        original_transaction = self.ledger.transaction
        transaction_calls = 0

        def fail_cleanup_transaction(*args, **kwargs):
            nonlocal transaction_calls
            transaction_calls += 1

            # Allow the initial code insertion, but fail the cleanup transaction.
            if transaction_calls == 2:
                raise RuntimeError("database cleanup unavailable")

            return original_transaction(*args, **kwargs)

        with mock.patch.object(
            self.ledger,
            "transaction",
            side_effect=fail_cleanup_transaction,
        ):
            with self.assertRaisesRegex(OSError, "SMTP unavailable") as caught:
                self.accounts.send_email_code(user, "verify")

        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(
            any("Email-code cleanup also failed" in note for note in notes)
        )

        # Cleanup failed, so the code remains active until separately invalidated
        # or expired. The original SMTP exception must still reach the caller.
        active_codes = self.ledger.db.execute(
            "SELECT COUNT(*) FROM email_codes "
            "WHERE user_id = ? AND purpose = 'verify' AND used = 0",
            (user,),
        ).fetchone()[0]
        self.assertEqual(active_codes, 1)

    def test_audit_failure_does_not_mask_email_delivery_error(self):
        user = self.accounts.register(
            "alice", PW, email="alice@example.com"
        )["user_id"]

        failed_mailer = mock.Mock()
        failed_mailer.send.side_effect = OSError("SMTP unavailable")
        self.accounts.mailer = failed_mailer

        with mock.patch.object(
            self.accounts,
            "audit",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "SMTP unavailable") as caught:
                self.accounts.send_email_code(user, "verify")

        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(
            any("Email-code failure audit also failed" in note for note in notes)
        )

        # Cleanup succeeded even though audit recording failed.
        active_codes = self.ledger.db.execute(
            "SELECT COUNT(*) FROM email_codes "
            "WHERE user_id = ? AND purpose = 'verify' AND used = 0",
            (user,),
        ).fetchone()[0]
        self.assertEqual(active_codes, 0)

    def test_registration_with_email_sends_a_code_and_verifies(self):
        user = self.accounts.register("alice", PW, email="Alice@Example.com")["user_id"]
        self.assertEqual(self.mailer.sent[-1][0], "Alice@Example.com")
        self.assertFalse(self.accounts.security_status(user)["email_verified"])
        self.accounts.verify_email(user, self.last_code())
        self.assertTrue(self.accounts.security_status(user)["email_verified"])

    def test_bad_emails_are_refused(self):
        for bad in ("nope", "a@b", "a@@b.com", "x" * 300 + "@a.com", 5, "a b@c.com"):
            with self.assertRaises(ValidationError):
                self.accounts.register("alice", PW, ip=str(bad)[:10], email=bad)

    def test_wrong_codes_burn_attempts_and_expiry_applies(self):
        user = self.accounts.register("alice", PW, email="a@example.com")["user_id"]
        real = self.last_code()
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.accounts.verify_email(user, "00000000" if real != "00000000" else "11111111")
        with self.assertRaises(AuthError):
            self.accounts.verify_email(user, real)                              # attempts exhausted, even for the right code
        self.accounts.send_email_code(user, "verify")
        self.clock.now += 901
        with self.assertRaises(AuthError):
            self.accounts.verify_email(user, self.last_code())                  # expired

    def test_resending_invalidates_the_previous_code(self):
        user = self.accounts.register("alice", PW, email="a@example.com")["user_id"]
        first = self.last_code()
        self.accounts.send_email_code(user, "verify")
        with self.assertRaises(AuthError):
            self.accounts.verify_email(user, first)
        self.accounts.verify_email(user, self.last_code())

    def test_verification_code_cannot_be_used_for_a_different_pending_address(self):
        user = self.accounts.register("alice", PW, email="original@example.com")["user_id"]
        self.accounts.verify_email(user, self.last_code())

        self.accounts.set_email(user, "first-pending@example.com", PW)
        stale_code = self.last_code()

        # Simulate a concurrent email-change request changing the pending target
        # after the code was created but before that code is submitted.
        with self.ledger.transaction():
            self.ledger.db.execute(
                "UPDATE users SET pending_email = ?, pending_email_key = ? WHERE user_id = ?",
                ("second-pending@example.com", "second-pending@example.com", user),
            )

        with self.assertRaises(AuthError):
            self.accounts.verify_email(user, stale_code)
        status = self.accounts.security_status(user)
        self.assertEqual(status["email"], "original@example.com")
        self.assertTrue(status["email_verified"])
        self.assertEqual(status["pending_email"], "second-pending@example.com")

        self.accounts.send_email_code(user, "verify")
        self.accounts.verify_email(user, self.last_code())
        status = self.accounts.security_status(user)
        self.assertEqual(status["email"], "second-pending@example.com")
        self.assertTrue(status["email_verified"])
        self.assertIsNone(status["pending_email"])

    def test_email_change_invalidates_recovery_codes_sent_to_old_address(self):
        user = self.accounts.register(
            "alice", PW, email="original@example.com"
        )["user_id"]
        self.accounts.verify_email(user, self.last_code())

        self.accounts.send_email_code(user, "reset")
        reset_code = self.last_code()
        self.accounts.send_email_code(user, "pin")
        pin_code = self.last_code()
        recovery_rows = self.ledger.db.execute(
            "SELECT purpose, used FROM email_codes "
            "WHERE user_id = ? AND purpose IN ('reset', 'pin')",
            (user,),
        ).fetchall()
        self.assertEqual({row["purpose"] for row in recovery_rows}, {"reset", "pin"})
        self.assertTrue(all(row["used"] == 0 for row in recovery_rows))

        self.accounts.set_email(user, "new@example.com", PW)
        self.accounts.verify_email(user, self.last_code())

        recovery_rows = self.ledger.db.execute(
            "SELECT purpose, used FROM email_codes "
            "WHERE user_id = ? AND purpose IN ('reset', 'pin')",
            (user,),
        ).fetchall()
        self.assertTrue(recovery_rows)
        self.assertTrue(all(row["used"] == 1 for row in recovery_rows))
        self.assertFalse(self.accounts._check_code(user, "reset", reset_code))
        self.assertFalse(self.accounts._check_code(user, "pin", pin_code))

    def test_recovery_code_for_noncurrent_email_is_rejected(self):
        user = self.accounts.register(
            "alice", PW, email="original@example.com"
        )["user_id"]
        self.accounts.verify_email(user, self.last_code())
        self.accounts.send_email_code(user, "reset")
        old_reset_code = self.last_code()
        old_code_row = self.ledger.db.execute(
            "SELECT code_hash, email_key FROM email_codes "
            "WHERE user_id = ? AND purpose = 'reset' AND used = 0",
            (user,),
        ).fetchone()

        # Simulate a stale recovery record surviving a partial/legacy migration.
        with self.ledger.transaction():
            self.ledger.db.execute(
                "UPDATE users SET email = ?, email_key = ? WHERE user_id = ?",
                ("new@example.com", "new@example.com", user),
            )

        self.assertNotEqual(old_code_row["email_key"], "new@example.com")
        self.assertFalse(self.accounts._check_code(user, "reset", old_reset_code))

    def test_unverified_email_cannot_be_squatted_and_only_one_account_can_verify_it(self):
        a = self.accounts.register("alice", PW, ip="1", email="shared@example.com")["user_id"]
        code_a = self.last_code()
        b = self.accounts.register("bobby", PW, ip="2", email="shared@example.com")["user_id"]      # allowed: not verified yet
        code_b = self.last_code()
        self.accounts.verify_email(a, code_a)
        with self.assertRaises(ConflictError):
            self.accounts.verify_email(b, code_b)

    def test_code_requests_are_rate_limited(self):
        user = self.accounts.register("alice", PW, email="a@example.com")["user_id"]
        for _ in range(4):
            self.accounts.send_email_code(user, "verify")
        with self.assertRaises(RateLimited):
            self.accounts.send_email_code(user, "verify")


class TestPasswordReset(Case):
    def setUp(self):
        super().setUp()
        self.user = self.accounts.register("alice", PW, email="a@example.com")["user_id"]
        self.accounts.verify_email(self.user, self.last_code())
        self.mailer.sent.clear()

    def test_reset_flow_changes_the_password_and_revokes_sessions(self):
        session = self.accounts.login("alice", PW)
        self.accounts.request_password_reset("A@example.com", "1.1.1.1")
        self.accounts.reset_password("a@example.com", self.last_code(), NEW_PW)
        self.assertIsNone(self.accounts.resolve(session["token"]))
        with self.assertRaises(AuthError):
            self.accounts.login("alice", PW)
        self.assertTrue(self.accounts.login("alice", NEW_PW))

    def test_request_never_reveals_whether_an_account_exists(self):
        self.accounts.request_password_reset("nobody@example.com", "1.1.1.1")
        self.assertEqual(self.mailer.sent, [])
        other = self.accounts.register("bobby", PW, ip="3", email="unverified@example.com")["user_id"]
        self.mailer.sent.clear()
        self.accounts.request_password_reset("unverified@example.com", "1.1.1.2")
        self.assertEqual(self.mailer.sent, [])                                  # unverified addresses never receive reset codes

    def test_code_is_single_use_and_guesses_are_limited(self):
        self.accounts.request_password_reset("a@example.com", "1.1.1.1")
        code = self.last_code()
        self.accounts.reset_password("a@example.com", code, NEW_PW)
        with self.assertRaises(AuthError):
            self.accounts.reset_password("a@example.com", code, "yet-another-password")
        self.accounts.request_password_reset("a@example.com", "1.1.1.3")
        for _ in range(4):                                                       # the reused code above was failure number one
            with self.assertRaises(AuthError):
                self.accounts.reset_password("a@example.com", "00000000", "yet-another-password")
        with self.assertRaises(RateLimited):
            self.accounts.reset_password("a@example.com", self.last_code(), "yet-another-password")

    def test_weak_new_passwords_are_refused_and_the_code_is_not_wasted_silently(self):
        self.accounts.request_password_reset("a@example.com", "1.1.1.1")
        with self.assertRaises(ValidationError):
            self.accounts.reset_password("a@example.com", self.last_code(), "short")

    def test_reset_by_email_turns_off_a_lost_authenticator(self):
        self.enable_2fa(self.user)                                             # then the phone is lost
        self.accounts.request_password_reset("a@example.com", "1.1.1.1")
        self.accounts.reset_password("a@example.com", self.last_code(), NEW_PW)
        self.assertTrue(self.accounts.login("alice", NEW_PW))                  # no authenticator code needed any more
        self.assertFalse(self.accounts.totp_status(self.user)["enabled"])

    def test_request_is_rate_limited_per_address(self):
        for _ in range(10):
            self.accounts.request_password_reset("nobody@example.com", "9.9.9.9")
        with self.assertRaises(RateLimited):
            self.accounts.request_password_reset("nobody@example.com", "9.9.9.9")


class TestKyc(Case):
    def setUp(self):
        super().setUp()
        self.kyc = KycStore(self.ledger, clock=self.clock)

    def test_tiers_drive_limits(self):
        self.assertEqual(self.kyc.tier("u1"), 0)
        tier0 = self.kyc.limits("u1", "NGN").max_amount_minor
        self.kyc.submit_basic("u1", "Ada Obi", "12345678901", auto_approve=True)
        tier1 = self.kyc.limits("u1", "NGN").max_amount_minor
        self.kyc.submit_enhanced("u1", "12 Marina Street, Lagos Island", auto_approve=True)
        self.assertLess(tier0, tier1)
        self.assertLess(tier1, self.kyc.limits("u1", "NGN").max_amount_minor)
        self.assertEqual(self.kyc.tier("u1"), 2)

    def test_nothing_is_approved_without_simulation_or_a_provider(self):
        status = self.kyc.submit_basic("u1", "Ada Obi", "12345678901", auto_approve=False)
        self.assertEqual((status["tier"], status["status"]), (0, "pending"))

    def test_bvn_is_not_stored_and_cannot_be_shared_between_accounts(self):
        self.kyc.submit_basic("u1", "Ada Obi", "12345678901", auto_approve=True)
        dump = " ".join(str(tuple(r)) for r in self.ledger.db.execute("SELECT * FROM kyc"))
        self.assertNotIn("12345678901", dump)
        self.assertEqual(self.kyc.status("u1")["bvn_last4"], "8901")
        with self.assertRaises(KycConflict):
            self.kyc.submit_basic("u2", "Chidi Eze", "12345678901", auto_approve=True)
        with self.assertRaises(KycError):
            self.kyc.submit_basic("u1", "Ada Obi", "12345678901", auto_approve=True)         # already verified

    def test_input_validation_and_ordering(self):
        for name, bvn in (("", "12345678901"), ("A", "12345678901"), ("Ada <b>", "12345678901"), ("Ada Obi", "1234"),
                          ("Ada Obi", "11111111111"), ("Ada Obi", "abcdefghijk"), ("Ada Obi", None)):
            with self.assertRaises(KycError, msg=(name, bvn)):
                self.kyc.submit_basic("u3", name, bvn, auto_approve=True)
        with self.assertRaises(KycError):
            self.kyc.submit_enhanced("u3", "12 Marina Street, Lagos", auto_approve=True)         # tier 1 first


class TestMailersAndFiles(unittest.TestCase):
    def test_console_mailer_appends_to_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail" / "outbox.log"
            ConsoleMailer(path).send("a@example.com", "Subject", "Body text")
            self.assertIn("Body text", path.read_text())

    def test_smtp_mailer_uses_starttls_and_login(self):
        with mock.patch("app.accounts.mail.smtplib.SMTP") as smtp_class:
            SmtpMailer("smtp.example.com", 587, "no-reply@example.com", "user", "secret").send("a@example.com", "Hi", "Body")
            smtp = smtp_class.return_value.__enter__.return_value
            smtp.starttls.assert_called_once()
            smtp.login.assert_called_once_with("user", "secret")
            sent = smtp.send_message.call_args[0][0]
            self.assertEqual((sent["To"], sent["Subject"]), ("a@example.com", "Hi"))

    def test_database_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform.sqlite3"
            ledger = WalletLedger(path)
            ledger.close()
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_database_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.sqlite3"
            target.write_bytes(b"")
            target.chmod(0o600)

            link = Path(tmp) / "platform.sqlite3"
            link.symlink_to(target)

            with self.assertRaises(PermissionError):
                WalletLedger(link)

    def test_database_rejects_non_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform.sqlite3"
            path.mkdir()

            with self.assertRaises(PermissionError):
                WalletLedger(path)

    def test_database_permission_failure_stops_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform.sqlite3"

            with mock.patch(
                "app.ledger.wallet_ledger.os.fchmod",
                side_effect=PermissionError("permission denied"),
            ):
                with mock.patch(
                    "app.ledger.wallet_ledger.sqlite3.connect"
                ) as connect:
                    with self.assertRaises(PermissionError):
                        WalletLedger(path)

                    connect.assert_not_called()



class TestLoadSecret(unittest.TestCase):
    def test_load_secret_creates_and_reuses_owner_only_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            first = load_secret(path)
            self.assertEqual(len(first), 32)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_secret(path), first)

    def test_load_secret_rejects_permissive_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            path.write_bytes(b"s" * 32)
            path.chmod(0o644)
            with self.assertRaises(PermissionError):
                load_secret(path)

    def test_load_secret_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.key"
            target.write_bytes(b"s" * 32)
            target.chmod(0o600)
            link = Path(tmp) / "secret.key"
            link.symlink_to(target)
            with self.assertRaises(PermissionError):
                load_secret(link)

    def test_load_secret_rejects_non_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            path.mkdir()
            with self.assertRaises(PermissionError):
                load_secret(path)

    def test_load_secret_rejects_truncated_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            path.write_bytes(b"short")
            path.chmod(0o600)
            with self.assertRaises(RuntimeError):
                load_secret(path)



class TestAccountStoreSecretValidation(unittest.TestCase):
    def test_missing_secret_generates_a_32_byte_ephemeral_key(self):
        ledger = WalletLedger()
        try:
            store = AccountStore(ledger, password_iterations=1000)
            self.assertIsInstance(store.pin_pepper, bytes)
            self.assertEqual(len(store.pin_pepper), 32)
        finally:
            ledger.close()

    def test_explicit_invalid_secrets_are_rejected(self):
        for invalid in (b"", b"short", b"x" * 31, b"x" * 33, "x" * 32, bytearray(b"x" * 32)):
            ledger = WalletLedger()
            try:
                with self.subTest(secret_type=type(invalid).__name__, length=len(invalid)):
                    with self.assertRaises(ValueError):
                        AccountStore(
                            ledger,
                            password_iterations=1000,
                            pin_pepper=invalid,
                        )
            finally:
                ledger.close()


class TestEmailChangeRace(Case):
    def test_stale_email_change_request_cannot_create_or_send_code(self):
        user = self.accounts.register(
            "alice", PW, email="original@example.com"
        )["user_id"]

        with self.ledger.transaction():
            self.ledger.db.execute(
                "UPDATE users SET pending_email = ?, pending_email_key = ? WHERE user_id = ?",
                ("second@example.com", "second@example.com", user),
            )

        sent_before = len(self.mailer.sent)
        codes_before = self.ledger.db.execute(
            "SELECT COUNT(*) FROM email_codes WHERE user_id = ? AND purpose = 'verify'",
            (user,),
        ).fetchone()[0]

        with self.assertRaises(ConflictError):
            self.accounts.send_email_code(
                user,
                "verify",
                expected_email_key="first@example.com",
            )

        sent_after = len(self.mailer.sent)
        codes_after = self.ledger.db.execute(
            "SELECT COUNT(*) FROM email_codes WHERE user_id = ? AND purpose = 'verify'",
            (user,),
        ).fetchone()[0]

        self.assertEqual(sent_after, sent_before)
        self.assertEqual(codes_after, codes_before)


if __name__ == "__main__":
    unittest.main()
