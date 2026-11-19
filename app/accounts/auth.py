"""Persistent accounts, sessions and audit trail.

* Customers are identified by an opaque ``user_id``; the ledger never sees usernames, so a username can never
  collide with a system ledger account.
* Passwords: PBKDF2-HMAC-SHA256 (parameters stored with each hash, upgraded on login), 12-128 characters,
  common/repetitive passwords and passwords containing the username rejected.
* Sessions: random 256-bit tokens, only their SHA-256 is stored; absolute and idle expiry; a per-session CSRF
  token; a cap on concurrent sessions; revocation on logout and password change.
* Login and registration are rate limited per username and per client address; unknown usernames cost the
  same as known ones and produce the same error.
* Every sensitive event is written to an append-only audit table.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from typing import Any, Callable

from ..ledger import SYSTEM_ACCOUNTS, WalletLedger, ensure_columns
from . import totp
from .limits import RateLimited, RateLimiter
from .mail import ConsoleMailer, Mailer

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
RESERVED_USERNAMES = frozenset(SYSTEM_ACCOUNTS | {"admin", "administrator", "root", "system", "support", "staff",
                                                  "null", "none", "anonymous", "operator", "service"})
COMMON_PASSWORDS = frozenset({
    "password1234", "password12345", "passwordpassword", "123456789012", "1234567890123", "qwertyuiop12",
    "qwertyuiopas", "letmein12345", "welcome12345", "administrator", "iloveyou1234", "changeme1234",
    "abcdefghijkl", "123412341234", "000000000000", "111111111111",
})
MAX_HASH_ITERATIONS = 5_000_000
PIN_PATTERN = re.compile(r"^\d{6}$")
WEAK_PINS = frozenset({"123123", "112233", "121212", "654321", "000123", "696969", "147258", "159753", "789456",
                       "123321", "111222", "202020", "010101", "000111", "999000", "100000", "123000", "246810"})


def validate_pin(pin: Any) -> str:
    if not isinstance(pin, str) or not PIN_PATTERN.fullmatch(pin):
        raise ValidationError("PIN must be exactly 6 digits")
    steps = {(int(b) - int(a)) % 10 for a, b in zip(pin, pin[1:])}
    if len(set(pin)) <= 2 or steps <= {1} or steps <= {9} or pin in WEAK_PINS:
        raise ValidationError("choose a PIN that is not a repeat, a sequence or a common pattern")
    return pin


class AccountError(Exception):
    """Base class for account errors."""


class ValidationError(AccountError, ValueError):
    pass


class AuthError(AccountError):
    pass


class ConflictError(AccountError):
    pass


class OtpRequired(AuthError):
    """The password was right but an authenticator code is needed."""


class PinNotSet(AccountError):
    """The customer has not set a transaction PIN yet."""


class PinRequired(AccountError):
    """A transaction PIN is needed for this action but none was supplied."""


class InvalidPin(AccountError):
    """The PIN was wrong."""


EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
CODE_TTL = 900
MAX_CODE_ATTEMPTS = 5


def validate_email(email: Any) -> str:
    if not isinstance(email, str) or len(email) > 254 or not EMAIL_PATTERN.fullmatch(email.strip()):
        raise ValidationError("enter a valid email address")
    return email.strip()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str, iterations: int) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> tuple[bool, int]:
    """Return (matches, iterations used by the stored hash)."""
    try:
        scheme, iterations_text, salt_b64, digest_b64 = encoded.split("$")
        iterations = int(iterations_text)
        if scheme != "pbkdf2_sha256" or not 1 <= iterations <= MAX_HASH_ITERATIONS:
            return False, 0
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False, 0
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected), iterations


def validate_username(username: Any) -> str:
    if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
        raise ValidationError("username must be 3-32 characters: letters, digits, '.', '_' or '-', "
                              "starting with a letter or digit")
    if username.casefold() in RESERVED_USERNAMES:
        raise ValidationError("that username is reserved")
    return username


def validate_password(password: Any, username: str = "") -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValidationError("password must be 12 to 128 characters")
    lowered = password.casefold()
    if len(set(password)) < 6:
        raise ValidationError("password is too repetitive")
    if lowered in COMMON_PASSWORDS:
        raise ValidationError("password is too common")
    if username and len(username) >= 3 and username.casefold() in lowered:
        raise ValidationError("password must not contain the username")
    return password


class AccountStore:
    def __init__(self, ledger: WalletLedger, *, password_iterations: int = 600_000, session_ttl: float = 8 * 3600,
                 session_idle: float = 3600, max_sessions: int = 5, login_max_failures: int = 5,
                 login_lock_seconds: float = 900, clock: Callable[[], float] = time.time,
                 limiter_clock: Callable[[], float] = time.monotonic, mailer: Mailer | None = None,
                 issuer: str = "Liquidity Lab", pin_pepper: bytes | None = None) -> None:
        self.ledger, self.db = ledger, ledger.db
        self.iterations, self.session_ttl, self.session_idle = password_iterations, session_ttl, session_idle
        self.max_sessions, self.clock = max_sessions, clock
        self.mailer, self.issuer = mailer or ConsoleMailer(), issuer
        self.audit_context: Callable[[], dict[str, Any]] = lambda: {}
        # One persistent server secret is domain-separated for independent PIN and email-code uses.
        # PlatformService loads it from the owner-only secret file; standalone stores get an ephemeral key.
        if pin_pepper is None:
            pin_pepper = secrets.token_bytes(32)
        elif not isinstance(pin_pepper, bytes) or len(pin_pepper) != 32:
            raise ValueError("pin_pepper must be exactly 32 bytes")
        self.pin_pepper = pin_pepper
        self.email_code_key = hmac.new(self.pin_pepper, b"liquidity-lab/email-code-hmac/v1", hashlib.sha256).digest()
        self.mail_limiter = RateLimiter(5, 3600, limiter_clock)
        self.user_limiter = RateLimiter(login_max_failures, login_lock_seconds, limiter_clock)
        self.ip_limiter = RateLimiter(login_max_failures * 6, login_lock_seconds, limiter_clock)
        self.register_limiter = RateLimiter(10, 3600, limiter_clock)
        self._dummy_hash = hash_password("not-a-real-password", password_iterations)
        with ledger.transaction():
            self.db.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
                csrf_token TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL,
                expires_at REAL NOT NULL, ip TEXT NOT NULL DEFAULT '', revoked INTEGER NOT NULL DEFAULT 0)""")
            ensure_columns(self.db, "users", {"email": "TEXT", "email_key": "TEXT", "email_verified": "INTEGER NOT NULL DEFAULT 0",
                                              "pending_email": "TEXT", "pending_email_key": "TEXT",
                                              "totp_secret": "TEXT", "totp_enabled": "INTEGER NOT NULL DEFAULT 0",
                                              "totp_last_step": "INTEGER NOT NULL DEFAULT 0",
                                              "pin_hash": "TEXT", "pin_set_at": "REAL"})
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_verified_email ON users(email_key) WHERE email_verified = 1")
            self.db.execute("DROP TABLE IF EXISTS recovery_codes")          # recovery codes were removed; email reset replaces them
            self.db.execute("""CREATE TABLE IF NOT EXISTS email_codes (
                code_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, purpose TEXT NOT NULL, email_key TEXT NOT NULL,
                code_hash TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, revoked, created_at)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, event TEXT NOT NULL,
                ip TEXT NOT NULL DEFAULT '', detail_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_events(user_id, event_id DESC)")
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END""")
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END""")

    # ---- audit ------------------------------------------------------------

    def audit(self, user_id: str | None, event: str, ip: str = "", **detail: Any) -> None:
        text = json.dumps({**self.audit_context(), **detail}, sort_keys=True, default=str)[:2000]
        with self.ledger.lock:
            self.db.execute("INSERT INTO audit_events(user_id, event, ip, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
                            (user_id, event, ip[:64], text, self.clock()))

    def recent_audit(self, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("limit must be an integer from 1 to 200")
        with self.ledger.lock:
            rows = self.db.execute("SELECT event_id, event, ip, detail_json, created_at FROM audit_events "
                                   "WHERE user_id = ? ORDER BY event_id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [{**{k: row[k] for k in ("event_id", "event", "ip", "created_at")},
                 "detail": json.loads(row["detail_json"])} for row in rows]

    # ---- registration and login -------------------------------------------

    def register(self, username: Any, password: Any, ip: str = "", email: Any = None) -> dict[str, str]:
        self.register_limiter.check_and_hit(f"ip:{ip}")
        validate_username(username)
        validate_password(password, username)
        email = validate_email(email) if email not in (None, "") else None
        user_id = "u_" + secrets.token_hex(16)
        encoded = hash_password(password, self.iterations)
        try:
            with self.ledger.transaction():
                self.db.execute("INSERT INTO users(user_id, username, username_key, password_hash, created_at, email, email_key) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, username, username.casefold(), encoded, self.clock(),
                                                                 email, email.casefold() if email else None))
        except sqlite3.IntegrityError:
            raise ConflictError("username is already taken") from None
        self.audit(user_id, "register", ip)
        if email:
            self.send_email_code(user_id, "verify", ip)
        return {"user_id": user_id, "username": username}

    def login(self, username: Any, password: Any, ip: str = "", otp: Any = None) -> dict[str, Any]:
        text = username.casefold()[:64] if isinstance(username, str) else ""
        user_key, ip_key = f"u:{text}", f"ip:{ip}"
        # Reject an already-blocked IP before charging a username attempt.
        retry_after = self.ip_limiter.blocked_for(ip_key)
        if retry_after:
            raise RateLimited(retry_after)

        # Check an existing username lockout without charging an attempt yet.
        # Only failed authentication should consume the username failure budget.
        retry_after = self.user_limiter.blocked_for(user_key)
        if retry_after:
            raise RateLimited(retry_after)

        with self.ledger.lock:
            row = self.db.execute("SELECT * FROM users WHERE username_key = ?", (text,)).fetchone() if text else None
        supplied = password if isinstance(password, str) and len(password) <= 1024 else ""
        matches, iterations = verify_password(supplied, row["password_hash"] if row else self._dummy_hash)
        if not (row and matches and not row["disabled"] and isinstance(password, str)):
            self.audit(row["user_id"] if row else None, "login_failed", ip)
            self.user_limiter.check_and_hit(user_key)
            self.ip_limiter.check_and_hit(ip_key)
            raise AuthError("invalid username or password")
        if row["totp_enabled"]:
            if otp in (None, ""):
                self.audit(row["user_id"], "login_failed", ip, reason="verification_code_required")
                self.ip_limiter.check_and_hit(ip_key)
                raise OtpRequired("verification code required")
            if not self._consume_totp(row["user_id"], otp):
                self.audit(row["user_id"], "login_failed", ip, reason="verification_code")
                self.user_limiter.check_and_hit(user_key)
                self.ip_limiter.check_and_hit(ip_key)
                raise AuthError("invalid username, password or verification code")
        self.user_limiter.reset(user_key)
        if iterations < self.iterations:     # upgrade an older hash now that we hold the password
            with self.ledger.transaction():
                self.db.execute("UPDATE users SET password_hash = ? WHERE user_id = ?",
                                (hash_password(password, self.iterations), row["user_id"]))
        session = self._create_session(row["user_id"], ip)
        self.audit(row["user_id"], "login", ip)
        return {**session, "user": {"user_id": row["user_id"], "username": row["username"]}}

    def _create_session(self, user_id: str, ip: str) -> dict[str, Any]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = self.clock()
        with self.ledger.transaction():
            self.db.execute("DELETE FROM sessions WHERE expires_at < ? OR (revoked = 1 AND created_at < ?)",
                            (now - 86_400, now - 86_400))
            self.db.execute("INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, last_seen, expires_at, ip) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, now, now,
                             now + self.session_ttl, ip[:64]))
            self.db.execute(
                "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0 AND token_hash NOT IN "
                "(SELECT token_hash FROM sessions WHERE user_id = ? AND revoked = 0 ORDER BY created_at DESC LIMIT ?)",
                (user_id, user_id, self.max_sessions))
        return {"token": token, "csrf_token": csrf, "expires_at": now + self.session_ttl}

    # ---- authenticator app (optional, login only) -----------------------------------

    def _consume_totp(self, user_id: str, code: Any) -> bool:
        """Accept an authenticator code once per 30-second step."""
        if not isinstance(code, str):
            return False
        code = code.replace(" ", "")
        with self.ledger.transaction():
            row = self.db.execute("SELECT totp_secret, totp_enabled, totp_last_step FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None or not row["totp_enabled"]:
                return False
            step = totp.verify(row["totp_secret"], code, self.clock(), row["totp_last_step"])
            if step is None:
                return False
            self.db.execute("UPDATE users SET totp_last_step = ? WHERE user_id = ?", (step, user_id))
            return True

    def totp_status(self, user_id: str) -> dict[str, Any]:
        with self.ledger.lock:
            row = self.db.execute("SELECT totp_enabled FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return {"enabled": bool(row and row["totp_enabled"])}

    def _reauthenticate(self, user_id: str, password: Any, otp: Any = None, *, action: str = "sensitive_action",
                        ip: str = "") -> None:
        """Require fresh credentials before changing account-recovery or MFA settings."""
        key = f"reauth:{user_id}"
        self.user_limiter.check_and_hit(key)
        with self.ledger.lock:
            row = self.db.execute("SELECT password_hash, totp_enabled FROM users WHERE user_id = ?", (user_id,)).fetchone()
        valid = bool(row and isinstance(password, str) and verify_password(password[:1024], row["password_hash"])[0])
        if valid and row["totp_enabled"]:
            valid = self._consume_totp(user_id, otp)
        if not valid:
            self.audit(user_id, "sensitive_action_reauthentication_failed", ip, action=action)
            raise AuthError("password or authenticator code is incorrect")
        self.user_limiter.reset(key)
        self.audit(user_id, "sensitive_action_reauthenticated", ip, action=action)

    def totp_begin(self, user_id: str, password: Any, ip: str = "") -> dict[str, str]:
        with self.ledger.lock:
            current = self.db.execute("SELECT totp_enabled FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if current is None:
            raise AuthError("unknown user")
        if current["totp_enabled"]:
            raise ConflictError("the authenticator app is already enabled")
        self._reauthenticate(user_id, password, action="totp_enrollment", ip=ip)
        secret = totp.generate_secret()
        with self.ledger.transaction():
            row = self.db.execute("SELECT username, totp_enabled FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                raise AuthError("unknown user")
            if row["totp_enabled"]:
                raise ConflictError("the authenticator app is already enabled")
            self.db.execute("UPDATE users SET totp_secret = ?, totp_last_step = 0 WHERE user_id = ?", (secret, user_id))
        return {"secret": secret, "otpauth_uri": totp.otpauth_uri(secret, row["username"], self.issuer)}

    def totp_confirm(self, user_id: str, code: Any, ip: str = "") -> None:
        """Turn the authenticator on after the user proves their app works."""
        key = f"otp:{user_id}"
        self.user_limiter.check_and_hit(key)
        with self.ledger.transaction():
            row = self.db.execute("SELECT totp_secret, totp_enabled FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None or not row["totp_secret"] or row["totp_enabled"]:
                raise ConflictError("start authenticator setup first")
            step = totp.verify(row["totp_secret"], code if isinstance(code, str) else "", self.clock())
            if step is None:
                raise AuthError("that code is not valid; check your authenticator app's clock")
            # Enrollment consumes the proof just like a login does. Recording the
            # accepted step prevents the same authenticator code being replayed.
            self.db.execute(
                "UPDATE users SET totp_enabled = 1, totp_last_step = ? WHERE user_id = ?",
                (step, user_id),
            )
        self.user_limiter.reset(key)
        self.audit(user_id, "totp_enabled", ip)

    def totp_disable(self, user_id: str, password: Any, code: Any, ip: str = "") -> None:
        """Turn the authenticator off (password and a current code). If the device is lost, a password reset by
        email turns it off instead."""
        with self.ledger.lock:
            row = self.db.execute("SELECT password_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
        key = f"pw:{user_id}"
        self.user_limiter.check_and_hit(key)
        if row is None or not isinstance(password, str) or not verify_password(password[:1024], row["password_hash"])[0]:
            raise AuthError("password is incorrect")
        if not self._consume_totp(user_id, code):
            raise AuthError("invalid verification code")
        with self.ledger.transaction():
            self.db.execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL, totp_last_step = 0 WHERE user_id = ?", (user_id,))
        self.user_limiter.reset(key)
        self.audit(user_id, "totp_disabled", ip)

    # ---- transaction PIN -------------------------------------------------------------

    def _encode_pin(self, pin: str) -> str:
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt + self.pin_pepper, self.iterations)
        return f"pin1${self.iterations}${_b64(salt)}${_b64(digest)}"

    def _pin_matches(self, pin: str, encoded: str) -> bool:
        try:
            _, iterations, salt_b64, digest_b64 = encoded.split("$")
            iterations = int(iterations)
            if not 1 <= iterations <= MAX_HASH_ITERATIONS:
                return False
            salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pin.encode(), salt + self.pin_pepper, iterations), expected)

    def pin_status(self, user_id: str) -> dict[str, Any]:
        with self.ledger.lock:
            row = self.db.execute("SELECT pin_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return {"set": bool(row and row["pin_hash"])}

    def _check_password(self, user_id: str, password: Any) -> None:
        key = f"pw:{user_id}"
        self.user_limiter.check_and_hit(key)
        with self.ledger.lock:
            row = self.db.execute("SELECT password_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None or not isinstance(password, str) or not verify_password(password[:1024], row["password_hash"])[0]:
            raise AuthError("password is incorrect")
        self.user_limiter.reset(key)

    def _store_pin(self, user_id: str, pin: str) -> None:
        encoded = self._encode_pin(pin)
        with self.ledger.transaction():
            self.db.execute("UPDATE users SET pin_hash = ?, pin_set_at = ? WHERE user_id = ?", (encoded, self.clock(), user_id))

    def set_pin(self, user_id: str, password: Any, pin: Any, ip: str = "") -> None:
        """First-time PIN. Needs the account password so a hijacked session cannot choose the PIN."""
        validate_pin(pin)
        self._check_password(user_id, password)
        if self.pin_status(user_id)["set"]:
            raise ConflictError("a PIN is already set; change it, or reset it by email if you forgot it")
        self._store_pin(user_id, pin)
        self.audit(user_id, "pin_set", ip)

    def change_pin(self, user_id: str, current_pin: Any, new_pin: Any, ip: str = "") -> None:
        validate_pin(new_pin)
        self.verify_pin(user_id, current_pin, ip)
        if new_pin == current_pin:
            raise ValidationError("the new PIN must differ from the current one")
        self._store_pin(user_id, new_pin)
        self.audit(user_id, "pin_changed", ip)

    def verify_pin(self, user_id: str, pin: Any, ip: str = "") -> None:
        """Authorise one transaction. Five wrong PINs lock PIN use for 15 minutes."""
        with self.ledger.lock:
            row = self.db.execute("SELECT pin_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None or not row["pin_hash"]:
            raise PinNotSet("set your transaction PIN first")
        key = f"pin:{user_id}"
        if pin in (None, ""):
            raise PinRequired("enter your transaction PIN")
        self.user_limiter.check_and_hit(key)
        if not isinstance(pin, str) or not PIN_PATTERN.fullmatch(pin) or not self._pin_matches(pin, row["pin_hash"]):
            self.audit(user_id, "pin_failed", ip)
            raise InvalidPin("incorrect PIN")
        self.user_limiter.reset(key)

    def request_pin_reset(self, user_id: str, ip: str = "") -> None:
        """Email a code that lets the customer choose a new PIN (needs a verified email)."""
        status = self.security_status(user_id)
        if not status["email_verified"]:
            raise ValidationError("verify your email first; it is how a forgotten PIN is reset")
        self.send_email_code(user_id, "pin", ip)

    def reset_pin(self, user_id: str, code: Any, password: Any, new_pin: Any, ip: str = "") -> None:
        validate_pin(new_pin)
        self._check_password(user_id, password)
        if not self._check_code(user_id, "pin", code):
            raise AuthError("invalid or expired code")
        self._store_pin(user_id, new_pin)
        self.user_limiter.reset(f"pin:{user_id}")
        self.audit(user_id, "pin_reset", ip)

    # ---- email verification and password reset ----------------------------------

    def security_status(self, user_id: str) -> dict[str, Any]:
        with self.ledger.lock:
            row = self.db.execute(
                "SELECT email, email_verified, pending_email FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return {"email": row["email"] if row else None, "email_verified": bool(row and row["email_verified"]),
                "pending_email": row["pending_email"] if row else None,
                "totp": self.totp_status(user_id), "pin": self.pin_status(user_id)}

    def _hash_code(self, code: str, user_id: str, purpose: str) -> str:
        """Keyed digest: a database-only leak cannot cheaply enumerate an eight-digit code."""
        message = f"{purpose}:{user_id}:{code}".encode("utf-8")
        return hmac.new(self.email_code_key, message, hashlib.sha256).hexdigest()

    def send_email_code(
        self,
        user_id: str,
        purpose: str,
        ip: str = "",
        *,
        expected_email_key: str | None = None,
    ) -> None:
        if expected_email_key is not None and purpose != "verify":
            raise ValueError("expected_email_key is only valid for email verification")

        with self.ledger.lock:
            row = self.db.execute(
                "SELECT email, email_key, pending_email, pending_email_key FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()

        if row is None:
            raise ValidationError("unknown user")

        if purpose == "verify" and row["pending_email"]:
            destination, destination_key = row["pending_email"], row["pending_email_key"]
        else:
            destination, destination_key = row["email"], row["email_key"]

        if not destination:
            raise ValidationError("add an email address first")
        if expected_email_key is not None and destination_key != expected_email_key:
            raise ConflictError("pending email changed; request a new verification code")

        self.mail_limiter.check_and_hit(f"mail:{destination_key}")
        code = f"{secrets.randbelow(10 ** 8):08d}"

        # Re-read and validate inside the write transaction. This prevents a
        # concurrent email change from redirecting a code to another address.
        with self.ledger.transaction():
            current = self.db.execute(
                "SELECT email, email_key, pending_email, pending_email_key FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if current is None:
                raise ValidationError("unknown user")

            if purpose == "verify" and current["pending_email"]:
                current_destination = current["pending_email"]
                current_destination_key = current["pending_email_key"]
            else:
                current_destination = current["email"]
                current_destination_key = current["email_key"]

            if (
                not current_destination
                or current_destination_key != destination_key
                or (
                    expected_email_key is not None
                    and current_destination_key != expected_email_key
                )
            ):
                raise ConflictError("email address changed; request a new verification code")

            self.db.execute(
                "UPDATE email_codes SET used = 1 "
                "WHERE user_id = ? AND purpose = ? AND used = 0",
                (user_id, purpose),
            )
            cursor = self.db.execute(
                "INSERT INTO email_codes(user_id, purpose, email_key, code_hash, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    purpose,
                    destination_key,
                    self._hash_code(code, user_id, purpose),
                    self.clock() + CODE_TTL,
                ),
            )
            code_id = cursor.lastrowid

        subject = {
            "verify": "Your verification code",
            "reset": "Your password reset code",
            "pin": "Your PIN reset code",
        }[purpose]
        try:
            self.mailer.send(
                destination,
                subject,
                f"Your code is {code}. It expires in {CODE_TTL // 60} minutes. "
                "If you did not ask for it, ignore this message and do not share the code.",
            )
        except Exception as delivery_error:
            # A failed or ambiguous delivery must not leave an unseen code usable.
            # Retire only this code: a concurrent resend may have created a newer one.
            try:
                with self.ledger.transaction():
                    self.db.execute(
                        "UPDATE email_codes SET used = 1 WHERE code_id = ? AND used = 0",
                        (code_id,),
                    )
            except Exception as cleanup_error:
                # Preserve the original delivery exception.
                if hasattr(delivery_error, "add_note"):
                    delivery_error.add_note(
                        f"Email-code cleanup also failed: {type(cleanup_error).__name__}"
                    )

            try:
                self.audit(user_id, "email_code_delivery_failed", ip, purpose=purpose)
            except Exception as audit_error:
                # Auditing must not replace the original delivery exception either.
                if hasattr(delivery_error, "add_note"):
                    delivery_error.add_note(
                        f"Email-code failure audit also failed: {type(audit_error).__name__}"
                    )

            raise

        self.audit(user_id, "email_code_sent", ip, purpose=purpose)

    def set_email(self, user_id: str, email: Any, password: Any, otp: Any = None, ip: str = "") -> None:
        self._reauthenticate(user_id, password, otp, action="email_change", ip=ip)
        email = validate_email(email)
        email_key = email.casefold()
        with self.ledger.transaction():
            row = self.db.execute("SELECT email, email_key, email_verified FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                raise AuthError("unknown user")
            if row["email"] and row["email_key"] == email_key and row["email_verified"]:
                raise ConflictError("that email address is already your verified email")
            self.db.execute(
                "UPDATE users SET pending_email = ?, pending_email_key = ? WHERE user_id = ?",
                (email, email_key, user_id),
            )
        self.audit(user_id, "email_change_requested", ip)
        self.send_email_code(user_id, "verify", ip, expected_email_key=email_key)

    def _check_code(self, user_id: str, purpose: str, code: Any) -> bool:
        """Validate (and consume) the latest unexpired code; wrong guesses burn attempts."""
        if not isinstance(code, str) or not re.fullmatch(r"\d{8}", code.strip()):
            return False
        with self.ledger.transaction():
            row = self.db.execute(
                "SELECT email_codes.* FROM email_codes "
                "JOIN users ON users.user_id = email_codes.user_id "
                "WHERE email_codes.user_id = ? AND email_codes.purpose = ? "
                "AND email_codes.used = 0 AND users.email_verified = 1 "
                "AND email_codes.email_key = users.email_key "
                "ORDER BY email_codes.code_id DESC LIMIT 1",
                (user_id, purpose),
            ).fetchone()
            if row is None or row["expires_at"] < self.clock() or row["attempts"] >= MAX_CODE_ATTEMPTS:
                return False
            if not hmac.compare_digest(row["code_hash"], self._hash_code(code.strip(), user_id, purpose)):
                self.db.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE code_id = ?", (row["code_id"],))
                return False
            self.db.execute("UPDATE email_codes SET used = 1 WHERE code_id = ?", (row["code_id"],))
            return True

    def verify_email(self, user_id: str, code: Any, ip: str = "") -> None:
        """Verify only the exact address to which the current code was sent.

        The code/address comparison and promotion happen in one transaction so a
        concurrent email-change request cannot swap the pending address between them.
        """
        if not isinstance(code, str) or not re.fullmatch(r"\d{8}", code.strip()):
            raise AuthError("invalid or expired code")

        verified = False
        try:
            with self.ledger.transaction():
                user = self.db.execute(
                    "SELECT email_key, pending_email_key FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
                row = self.db.execute(
                    "SELECT * FROM email_codes WHERE user_id = ? AND purpose = 'verify' AND used = 0 "
                    "ORDER BY code_id DESC LIMIT 1", (user_id,)
                ).fetchone()
                if user is not None and row is not None and row["expires_at"] >= self.clock() and row["attempts"] < MAX_CODE_ATTEMPTS:
                    expected_email_key = user["pending_email_key"] or user["email_key"]
                    if not expected_email_key or row["email_key"] != expected_email_key:
                        # A newer address is pending. Retire the stale code without promoting it.
                        self.db.execute("UPDATE email_codes SET used = 1 WHERE code_id = ?", (row["code_id"],))
                    elif not hmac.compare_digest(row["code_hash"], self._hash_code(code.strip(), user_id, "verify")):
                        self.db.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE code_id = ?", (row["code_id"],))
                    else:
                        self.db.execute("UPDATE email_codes SET used = 1 WHERE code_id = ?", (row["code_id"],))
                        self.db.execute(
                            "UPDATE users SET email = COALESCE(pending_email, email), "
                            "email_key = COALESCE(pending_email_key, email_key), email_verified = 1, "
                            "pending_email = NULL, pending_email_key = NULL WHERE user_id = ?",
                            (user_id,),
                        )
                        # Recovery codes were delivered to the previous verified
                        # address. Retire them in the same transaction as promotion.
                        self.db.execute(
                            "UPDATE email_codes SET used = 1 "
                            "WHERE user_id = ? AND purpose IN ('reset', 'pin') AND used = 0",
                            (user_id,),
                        )
                        verified = True
        except sqlite3.IntegrityError:
            raise ConflictError("that email is already verified on another account") from None
        if not verified:
            raise AuthError("invalid or expired code")
        self.audit(user_id, "email_verified", ip)

    def request_password_reset(self, email: Any, ip: str = "") -> None:
        """Always returns quietly so the response never reveals whether an account exists."""
        key = email.strip().casefold() if isinstance(email, str) else ""
        self.register_limiter.check_and_hit(f"reset-ip:{ip}")
        with self.ledger.lock:
            row = self.db.execute("SELECT user_id FROM users WHERE email_key = ? AND email_verified = 1", (key,)).fetchone() if key else None
        if row is not None:
            try:
                self.send_email_code(row["user_id"], "reset", ip)
            except RateLimited:
                pass

    def reset_password(self, email: Any, code: Any, new_password: Any, ip: str = "") -> None:
        key = email.strip().casefold() if isinstance(email, str) else ""
        limit_key = f"reset:{key}"
        self.user_limiter.check_and_hit(limit_key)
        with self.ledger.lock:
            row = self.db.execute("SELECT user_id, username FROM users WHERE email_key = ? AND email_verified = 1", (key,)).fetchone() if key else None
        if row is None:
            raise AuthError("invalid or expired code")
        validate_password(new_password, row["username"])
        if not self._check_code(row["user_id"], "reset", code):
            raise AuthError("invalid or expired code")
        encoded = hash_password(new_password, self.iterations)
        with self.ledger.transaction():
            self.db.execute("UPDATE users SET password_hash = ?, totp_enabled = 0, totp_secret = NULL, totp_last_step = 0 "
                            "WHERE user_id = ?", (encoded, row["user_id"]))
            self.db.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (row["user_id"],))
        self.user_limiter.reset(limit_key)
        self.audit(row["user_id"], "password_reset", ip)

    # ---- sessions ---------------------------------------------------------

    def resolve(self, token: Any) -> dict[str, Any] | None:
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = self.clock()
        with self.ledger.lock:
            row = self.db.execute(
                "SELECT s.*, u.username, u.disabled FROM sessions s JOIN users u ON u.user_id = s.user_id "
                "WHERE s.token_hash = ?", (digest,)).fetchone()
            if row is None or row["revoked"] or row["disabled"]:
                return None
            if now >= row["expires_at"] or now - row["last_seen"] > self.session_idle:
                self.db.execute("UPDATE sessions SET revoked = 1 WHERE token_hash = ?", (digest,))
                return None
            if now - row["last_seen"] > 30:
                self.db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, digest))
        return {"user_id": row["user_id"], "username": row["username"], "csrf_token": row["csrf_token"],
                "expires_at": row["expires_at"], "token_hash": digest}

    def logout(self, token: str) -> None:
        with self.ledger.lock:
            self.db.execute("UPDATE sessions SET revoked = 1 WHERE token_hash = ?",
                            (hashlib.sha256(token.encode()).hexdigest(),))

    def logout_all(self, user_id: str) -> None:
        with self.ledger.lock:
            self.db.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))

    def change_password(self, user_id: str, current: Any, new: Any, keep_token_hash: str | None = None,
                        ip: str = "") -> None:
        key = f"pw:{user_id}"
        self.user_limiter.check_and_hit(key)
        with self.ledger.lock:
            row = self.db.execute("SELECT username, password_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None or not isinstance(current, str) or not verify_password(current[:1024], row["password_hash"])[0]:
            self.audit(user_id, "password_change_failed", ip)
            raise AuthError("current password is incorrect")
        validate_password(new, row["username"])
        if new == current:
            raise ValidationError("new password must differ from the current one")
        encoded = hash_password(new, self.iterations)
        with self.ledger.transaction():
            self.db.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", (encoded, user_id))
            self.db.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ? AND token_hash != ?",
                            (user_id, keep_token_hash or ""))
        self.user_limiter.reset(key)
        self.audit(user_id, "password_changed", ip)
