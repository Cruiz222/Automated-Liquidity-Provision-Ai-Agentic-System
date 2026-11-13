import sqlite3
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor

from app.accounts import (
    AccountStore, AuthError, ConflictError, RateLimited, RateLimiter, ValidationError, hash_password, verify_password,
)
from app.ledger import WalletLedger

PW = "correct-horse-battery"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def store(clock=None, **kw):
    ledger = WalletLedger()
    clock = clock or Clock()
    return AccountStore(ledger, password_iterations=1000, clock=clock, limiter_clock=clock, **kw), ledger, clock


class TestPasswords(unittest.TestCase):
    def test_hash_roundtrip_and_format(self):
        encoded = hash_password(PW, 1000)
        self.assertTrue(encoded.startswith("pbkdf2_sha256$1000$"))
        self.assertEqual(verify_password(PW, encoded), (True, 1000))
        self.assertFalse(verify_password("wrong", encoded)[0])
        self.assertNotEqual(hash_password(PW, 1000), encoded)            # salted

    def test_malformed_or_hostile_hashes_do_not_verify(self):
        for bad in ("", "x", "pbkdf2_sha256$abc$AA==$AA==", "pbkdf2_sha256$999999999$AA==$AA==", "md5$1$a$b"):
            self.assertEqual(verify_password(PW, bad), (False, 0))


class TestRegistration(unittest.TestCase):
    def setUp(self):
        self.accounts, self.ledger, self.clock = store()
        self.addCleanup(self.ledger.close)

    def test_username_rules_and_reserved_names(self):
        bad_names = ("ab", "a" * 33, "bad name", "-leading", "emoji\U0001F600x", "clearing", "Liquidity-Clearing", "ADMIN",
                     "fees", "withdrawal-hold", None, 5)
        for i, bad in enumerate(bad_names):
            with self.assertRaises(ValidationError, msg=str(bad)):
                self.accounts.register(bad, PW, ip=f"10.0.0.{i}")

    def test_password_rules(self):
        for i, bad in enumerate(("short", "a" * 129, "aaaaaaaaaaaaaaa", "password1234", "carol-is-here-123", None)):
            with self.assertRaises(ValidationError, msg=str(bad)):
                self.accounts.register("carol", bad, ip=f"10.1.0.{i}")

    def test_duplicate_usernames_are_case_insensitive(self):
        self.accounts.register("Alice", PW)
        with self.assertRaises(ConflictError):
            self.accounts.register("alice", PW)

    def test_user_ids_are_opaque_not_usernames(self):
        user = self.accounts.register("alice", PW)
        self.assertRegex(user["user_id"], r"^u_[0-9a-f]{32}$")

    def test_registration_is_rate_limited_per_address(self):
        for i in range(10):
            self.accounts.register(f"user{i:02d}", PW, ip="9.9.9.9")
        with self.assertRaises(RateLimited):
            self.accounts.register("user99", PW, ip="9.9.9.9")
        self.accounts.register("user98", PW, ip="8.8.8.8")


class TestLoginAndSessions(unittest.TestCase):
    def setUp(self):
        self.accounts, self.ledger, self.clock = store()
        self.addCleanup(self.ledger.close)
        self.user = self.accounts.register("alice", PW)["user_id"]

    def test_login_creates_session_and_only_the_hash_is_stored(self):
        session = self.accounts.login("ALICE", PW, "1.1.1.1")
        self.assertEqual(self.accounts.resolve(session["token"])["user_id"], self.user)
        stored = [r[0] for r in self.ledger.db.execute("SELECT token_hash FROM sessions")]
        self.assertNotIn(session["token"], stored)
        self.assertEqual(len(stored[0]), 64)

    def test_wrong_password_and_unknown_user_look_identical(self):
        errors = []
        for username, password in (("alice", "wrong-password-1"), ("nobody", PW)):
            with self.assertRaises(AuthError) as ctx:
                self.accounts.login(username, password, "2.2.2.2")
            errors.append(str(ctx.exception))
        self.assertEqual(errors[0], errors[1])

    def test_lockout_applies_to_known_and_unknown_names_alike(self):
        for name in ("alice", "ghost"):
            for _ in range(5):
                with self.assertRaises(AuthError):
                    self.accounts.login(name, "wrong-password-1", f"ip-{name}")
            with self.assertRaises(RateLimited) as ctx:
                self.accounts.login(name, PW, f"ip-{name}-2")          # even the right password is refused
            self.assertGreater(ctx.exception.retry_after, 0)

    def test_successful_logins_do_not_consume_ip_failure_allowance(self):
        for _ in range(35):
            self.accounts.login("alice", PW, "shared-ip")

    def test_successful_login_does_not_clear_shared_ip_failures(self):
        ip = "shared-ip"

        for i in range(29):
            with self.assertRaises(AuthError):
                self.accounts.login(f"ghost{i}", "wrong-password-1", ip)

        self.accounts.login("alice", PW, ip)

        with self.assertRaises(AuthError):
            self.accounts.login("another-ghost", "wrong-password-1", ip)

        with self.assertRaises(RateLimited):
            self.accounts.login("final-ghost", "wrong-password-1", ip)

    def test_blocked_ip_does_not_consume_username_allowance(self):
        ip = "blocked-shared-ip"
        self.accounts.ip_limiter = RateLimiter(1, 60, self.clock)

        with self.assertRaises(AuthError):
            self.accounts.login("ghost", "wrong-password-1", ip)

        with self.assertRaises(RateLimited):
            self.accounts.login("alice", PW, ip)

        self.assertEqual(
            self.accounts.user_limiter.blocked_for("u:alice"),
            0.0,
        )

    def test_concurrent_ip_failures_leave_ip_locked(self):
        from concurrent.futures import ThreadPoolExecutor

        ip = "concurrent-shared-ip"
        self.accounts.ip_limiter = RateLimiter(5, 60, self.clock)

        usernames = [f"unknown{i}" for i in range(12)]

        def attempt(username):
            try:
                self.accounts.login(username, "wrong-password-1", ip)
            except (AuthError, RateLimited):
                return
            self.fail("Invalid login unexpectedly succeeded")

        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(attempt, usernames))

        self.assertGreater(
            self.accounts.ip_limiter.blocked_for(f"ip:{ip}"), 0
        )

        with self.assertRaises(RateLimited):
            self.accounts.login("alice", PW, ip)

    def test_lockout_expires(self):
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.accounts.login("alice", "wrong-password-1", "3.3.3.3")
        self.clock.now += 901
        self.assertTrue(self.accounts.login("alice", PW, "3.3.3.3")["token"])

    def test_session_expiry_absolute_and_idle(self):
        absolute = self.accounts.login("alice", PW)["token"]
        self.clock.now += 3000
        self.assertIsNotNone(self.accounts.resolve(absolute))         # activity keeps it alive
        self.clock.now += 3000
        self.assertIsNotNone(self.accounts.resolve(absolute))
        self.clock.now += 3700                                        # idle for > 1 hour
        self.assertIsNone(self.accounts.resolve(absolute))
        fresh = self.accounts.login("alice", PW)["token"]
        for _ in range(9):                                            # stay active past the 8h absolute limit
            self.clock.now += 3500
            self.accounts.resolve(fresh)
        self.assertIsNone(self.accounts.resolve(fresh))

    def test_session_cap_revokes_oldest(self):
        tokens = []
        for _ in range(6):
            self.clock.now += 1
            tokens.append(self.accounts.login("alice", PW)["token"])
        self.assertIsNone(self.accounts.resolve(tokens[0]))
        self.assertIsNotNone(self.accounts.resolve(tokens[-1]))

    def test_logout_logout_all_and_password_change(self):
        a, b, c = (self.accounts.login("alice", PW)["token"] for _ in range(3))
        self.accounts.logout(a)
        self.assertIsNone(self.accounts.resolve(a))
        keep = self.accounts.resolve(b)
        with self.assertRaises(AuthError):
            self.accounts.change_password(self.user, "wrong-password-1", "another-long-password")
        self.accounts.change_password(self.user, PW, "another-long-password", keep["token_hash"])
        self.assertIsNotNone(self.accounts.resolve(b))                # current session survives
        self.assertIsNone(self.accounts.resolve(c))                   # others are revoked
        with self.assertRaises(AuthError):
            self.accounts.login("alice", PW)
        d = self.accounts.login("alice", "another-long-password")["token"]
        self.accounts.logout_all(self.user)
        self.assertIsNone(self.accounts.resolve(d))
        self.assertIsNone(self.accounts.resolve(b))

    def test_garbage_tokens_resolve_to_nothing(self):
        for token in (None, "", "a", "x" * 500, 12345, "../../etc/passwd" * 3):
            self.assertIsNone(self.accounts.resolve(token))

    def test_old_hashes_are_upgraded_on_login(self):
        weak = hash_password(PW, 10)
        self.ledger.db.execute("UPDATE users SET password_hash = ?", (weak,))
        self.accounts.login("alice", PW)
        stored = self.ledger.db.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertTrue(stored.startswith("pbkdf2_sha256$1000$"))

    def test_audit_trail_is_recorded_and_immutable(self):
        with self.assertRaises(AuthError):
            self.accounts.login("alice", "wrong-password-1", "4.4.4.4")
        self.accounts.login("alice", PW, "4.4.4.4")
        events = [e["event"] for e in self.accounts.recent_audit(self.user)]
        self.assertEqual(events[:2], ["login", "login_failed"])
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.db.execute("DELETE FROM audit_events")


class TestRateLimiter(unittest.TestCase):
    def test_check_and_hit_is_atomic_under_concurrent_requests(self):
        limiter = RateLimiter(5, 60, Clock(0))
        barrier = threading.Barrier(20)

        def attempt(_):
            barrier.wait()
            try:
                limiter.check_and_hit("shared-account")
                return True
            except RateLimited:
                return False

        with ThreadPoolExecutor(max_workers=20) as pool:
            accepted = list(pool.map(attempt, range(20)))
        self.assertEqual(sum(accepted), 5)
        self.assertEqual(len(limiter._events["shared-account"]), 5)

    def test_expired_key_cleanup_is_safe_and_reclaims_capacity(self):
        clock = Clock(0)
        limiter = RateLimiter(2, 10, clock, max_keys=3)
        for key in "abc":
            limiter.check_and_hit(key)
        clock.now = 11
        limiter.check_and_hit("new-key")
        self.assertEqual(set(limiter._events), {"new-key"})

    def test_key_cap_never_evicts_active_security_limits(self):
        clock = Clock(0)
        limiter = RateLimiter(2, 10, clock, max_keys=2)
        limiter.check_and_hit("protected-account")
        limiter.check_and_hit("protected-account")
        limiter.check_and_hit("second-account")

        # New attacker-controlled keys fail closed instead of evicting active keys.
        with self.assertRaises(RateLimited):
            limiter.check_and_hit("attacker-key-1")
        with self.assertRaises(RateLimited):
            limiter.hit("attacker-key-2")
        self.assertEqual(set(limiter._events), {"protected-account", "second-account"})
        self.assertEqual(len(limiter._events["protected-account"]), 2)
        with self.assertRaises(RateLimited):
            limiter.check_and_hit("protected-account")


if __name__ == "__main__":
    unittest.main()
