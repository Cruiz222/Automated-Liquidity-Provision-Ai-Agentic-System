"""Tests for app.accounts.totp and its use inside AccountStore."""
from __future__ import annotations

import base64
import unittest

from app.accounts import totp
from app.accounts.auth import AccountStore, AuthError, ConflictError, OtpRequired
from app.ledger import WalletLedger


def _code(secret: str, step: int) -> str:
    return totp._hotp(totp._key(secret), step)


class TotpPrimitiveTests(unittest.TestCase):
    def test_rfc6238_sha1_vector(self):
        # RFC 6238 Appendix B: ASCII "12345678901234567890", 6 digits, T0=0, step=30.
        secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
        for timestamp, expected in [
            (59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
            (1234567890, "005924"), (2000000000, "279037"),
        ]:
            with self.subTest(t=timestamp):
                self.assertEqual(_code(secret, timestamp // 30), expected)

    def test_generate_secret_is_base32_and_unique(self):
        a, b = totp.generate_secret(), totp.generate_secret()
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 32)
        totp._key(a)

    def test_otpauth_uri(self):
        secret = totp.generate_secret()
        uri = totp.otpauth_uri(secret, "alice", "Liquidity Lab")
        self.assertTrue(uri.startswith("otpauth://totp/Liquidity%20Lab%3Aalice?"))
        self.assertIn(f"secret={secret}", uri)
        self.assertIn("issuer=Liquidity%20Lab", uri)
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)

    def test_verify_accepts_skew_and_returns_step(self):
        secret = totp.generate_secret()
        now = 1_700_000_000.0
        step = totp.current_step(now)
        for offset in (-1, 0, 1):
            with self.subTest(offset=offset):
                self.assertEqual(totp.verify(secret, _code(secret, step + offset), now), step + offset)

    def test_verify_rejects_replay(self):
        secret = totp.generate_secret()
        now = 1_700_000_000.0
        step = totp.current_step(now)
        code = _code(secret, step)
        self.assertEqual(totp.verify(secret, code, now), step)
        self.assertIsNone(totp.verify(secret, code, now, last_step=step))

    def test_verify_rejects_bad_input(self):
        secret = totp.generate_secret()
        now = 1_700_000_000.0
        for bad in (None, "", "12345", "1234567", "abcdef", 123456):
            with self.subTest(code=bad):
                self.assertIsNone(totp.verify(secret, bad, now))  # type: ignore[arg-type]
        self.assertIsNone(totp.verify("not base32!!", _code(secret, 0), now))


class AccountStoreTotpTests(unittest.TestCase):
    def setUp(self):
        self.ledger = WalletLedger(":memory:")
        self.now = 1_700_000_000.0
        self.clock = lambda: self.now
        self.store = AccountStore(self.ledger, clock=self.clock, password_iterations=1000,
                                  pin_pepper=b"\x00" * 32)
        self.user = self.store.register("alice", "correct horse battery staple")["user_id"]

    def tearDown(self):
        self.ledger.close()

    def _enable(self) -> str:
        secret = self.store.totp_begin(self.user)["secret"]
        self.store.totp_confirm(self.user, _code(secret, totp.current_step(self.now)))
        return secret

    def test_enable_disable_flow(self):
        secret = self._enable()
        self.assertTrue(self.store.totp_status(self.user)["enabled"])
        self.store.totp_disable(self.user, "correct horse battery staple",
                                _code(secret, totp.current_step(self.now)))
        self.assertFalse(self.store.totp_status(self.user)["enabled"])

    def test_login_requires_otp_and_consumes_step(self):
        secret = self._enable()
        with self.assertRaises(OtpRequired):
            self.store.login("alice", "correct horse battery staple")
        step = totp.current_step(self.now)
        session = self.store.login("alice", "correct horse battery staple", otp=_code(secret, step))
        self.assertIn("token", session)
        self.now += 1
        with self.assertRaises(AuthError):
            self.store.login("alice", "correct horse battery staple", otp=_code(secret, step))
        self.now += totp.STEP
        self.store.login("alice", "correct horse battery staple", otp=_code(secret, totp.current_step(self.now)))

    def test_confirm_requires_pending_secret(self):
        with self.assertRaises(ConflictError):
            self.store.totp_confirm(self.user, "123456")

    def test_disable_requires_password_and_code(self):
        secret = self._enable()
        step = totp.current_step(self.now)
        with self.assertRaises(AuthError):
            self.store.totp_disable(self.user, "wrong password", _code(secret, step))
        with self.assertRaises(AuthError):
            self.store.totp_disable(self.user, "correct horse battery staple", "000000")
        self.store.totp_disable(self.user, "correct horse battery staple", _code(secret, step))
        self.assertFalse(self.store.totp_status(self.user)["enabled"])


if __name__ == "__main__":
    unittest.main()
