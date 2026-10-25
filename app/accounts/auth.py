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

from ..ledger import SYSTEM_ACCOUNTS, WalletLedger
from .limits import RateLimited, RateLimiter

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
RESERVED_USERNAMES = frozenset(SYSTEM_ACCOUNTS | {"admin", "administrator", "root", "system", "support", "staff",
                                                  "null", "none", "anonymous", "operator", "service"})
COMMON_PASSWORDS = frozenset({
    "password1234", "password12345", "passwordpassword", "123456789012", "1234567890123", "qwertyuiop12",
    "qwertyuiopas", "letmein12345", "welcome12345", "administrator", "iloveyou1234", "changeme1234",
    "abcdefghijkl", "123412341234", "000000000000", "111111111111",
})
MAX_HASH_ITERATIONS = 5_000_000


class AccountError(Exception):
    """Base class for account errors."""


class ValidationError(AccountError, ValueError):
    pass


class AuthError(AccountError):
    pass


class ConflictError(AccountError):
    pass


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
                 limiter_clock: Callable[[], float] = time.monotonic) -> None:
        self.ledger, self.db = ledger, ledger.db
        self.iterations, self.session_ttl, self.session_idle = password_iterations, session_ttl, session_idle
        self.max_sessions, self.clock = max_sessions, clock
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
        text = json.dumps(detail, sort_keys=True, default=str)[:2000]
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

    def register(self, username: Any, password: Any, ip: str = "") -> dict[str, str]:
        self.register_limiter.check_and_hit(f"ip:{ip}")
        validate_username(username)
        validate_password(password, username)
        user_id = "u_" + secrets.token_hex(16)
        encoded = hash_password(password, self.iterations)
        try:
            with self.ledger.transaction():
                self.db.execute("INSERT INTO users(user_id, username, username_key, password_hash, created_at) "
                                "VALUES (?, ?, ?, ?, ?)", (user_id, username, username.casefold(), encoded, self.clock()))
        except sqlite3.IntegrityError:
            raise ConflictError("username is already taken") from None
        self.audit(user_id, "register", ip)
        return {"user_id": user_id, "username": username}

    def login(self, username: Any, password: Any, ip: str = "") -> dict[str, Any]:
        text = username.casefold()[:64] if isinstance(username, str) else ""
        user_key, ip_key = f"u:{text}", f"ip:{ip}"
        retry = max(self.user_limiter.blocked_for(user_key), self.ip_limiter.blocked_for(ip_key))
        if retry > 0:
            raise RateLimited(retry)
        with self.ledger.lock:
            row = self.db.execute("SELECT * FROM users WHERE username_key = ?", (text,)).fetchone() if text else None
        supplied = password if isinstance(password, str) and len(password) <= 1024 else ""
        matches, iterations = verify_password(supplied, row["password_hash"] if row else self._dummy_hash)
        if not (row and matches and not row["disabled"] and isinstance(password, str)):
            self.user_limiter.hit(user_key)
            self.ip_limiter.hit(ip_key)
            self.audit(row["user_id"] if row else None, "login_failed", ip)
            raise AuthError("invalid username or password")
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
        retry = self.user_limiter.blocked_for(key)
        if retry > 0:
            raise RateLimited(retry)
        with self.ledger.lock:
            row = self.db.execute("SELECT username, password_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None or not isinstance(current, str) or not verify_password(current[:1024], row["password_hash"])[0]:
            self.user_limiter.hit(key)
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
