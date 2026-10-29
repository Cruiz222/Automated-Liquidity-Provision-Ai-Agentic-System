"""Hardened JSON API and static dashboard server (standard library only, loopback only).

Security model
* Session cookie (HttpOnly, SameSite=Strict, optionally Secure) plus a per-session CSRF token that every
  state-changing request must echo in ``X-CSRF-Token``; there are no bearer tokens or CORS headers.
* ``Host`` must be a loopback name for this port (DNS-rebinding defence); ``Origin``, when sent, must match.
* JSON bodies only: exact Content-Length, 16 KiB cap, strict per-route field whitelists and types.
* Money-moving calls require an ``Idempotency-Key`` (namespaced per user by the service).
* Per-IP and per-user rate limits, socket timeouts, no keep-alive, generic 500s without internals.
* Strict CSP (no inline script/style), nosniff, no-store, frame denial; static files come from a fixed allowlist.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import secrets
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from ..accounts import (
    AccountError, AuthError, ConflictError, InvalidPin, KycConflict, KycError, OtpRequired, PinNotSet, PinRequired,
    RateLimited, RateLimiter, ValidationError,
)
from ..config import Settings
from ..fx import QuoteError, QuoteExpired, RateError
from ..ledger import (
    IdempotencyConflict, InsufficientFunds, InvalidOperation, InvalidTransition, LedgerError, LimitExceeded,
)
from ..dex.http import ProviderError
from ..payouts import AmbiguousBank, PayoutAccountNotFound
from ..portfolio import PositionNotFound
from ..live import WebhookError, WebhookRejected
from ..service import LiveUnavailable, NotFound, PlatformService, SimulationDisabled

log = logging.getLogger("app.api")
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/static/app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
MAX_BODY = 16 * 1024
MODAL_PREFIXES = ("/api/wallet", "/api/withdrawals", "/api/quotes", "/api/positions")   # routes that act on demo or live books
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")
ID_RE = r"([A-Za-z0-9_.:-]{1,120})"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.headers, self.details = status, code, message, headers or {}, details or {}


def translate(error: Exception) -> ApiError | None:
    """Map domain exceptions to HTTP errors. Unknown exceptions return None (generic 500)."""
    if isinstance(error, ApiError):
        return error
    if isinstance(error, AmbiguousBank):
        return ApiError(409, "ambiguous_bank", str(error), details={"candidates": error.candidates})
    if isinstance(error, LiveUnavailable):
        return ApiError(403, "live_unavailable", str(error), details={"requirements": error.requirements})
    if isinstance(error, WebhookRejected):
        return ApiError(422, "webhook_rejected", str(error))
    if isinstance(error, WebhookError):
        return ApiError(401, "bad_signature", "invalid webhook signature")
    if isinstance(error, ProviderError):
        return ApiError(503, "data_unavailable", "market data is temporarily unavailable")
    if isinstance(error, PinNotSet):
        return ApiError(403, "pin_not_set", str(error))
    if isinstance(error, PinRequired):
        return ApiError(403, "pin_required", str(error))
    if isinstance(error, InvalidPin):
        return ApiError(403, "invalid_pin", str(error))
    if isinstance(error, OtpRequired):
        return ApiError(401, "otp_required", str(error))
    if isinstance(error, (KycConflict,)):
        return ApiError(409, "conflict", str(error))
    if isinstance(error, KycError):
        return ApiError(400, "invalid_request", str(error))
    if isinstance(error, RateLimited):
        return ApiError(429, "rate_limited", str(error), {"Retry-After": str(error.retry_after)})
    if isinstance(error, AuthError):
        return ApiError(401, "unauthorized", str(error))
    if isinstance(error, ConflictError):
        return ApiError(409, "conflict", str(error))
    if isinstance(error, (ValidationError, InvalidOperation)):
        return ApiError(400, "invalid_request", str(error))
    if isinstance(error, (NotFound, PositionNotFound, PayoutAccountNotFound)):
        return ApiError(404, "not_found", "resource not found")
    if isinstance(error, SimulationDisabled):
        return ApiError(403, "simulation_disabled", str(error))
    if isinstance(error, (InsufficientFunds, LimitExceeded)):
        return ApiError(422, "rejected", str(error))
    if isinstance(error, (IdempotencyConflict, InvalidTransition)):
        return ApiError(409, "conflict", str(error))
    if isinstance(error, QuoteExpired):
        return ApiError(410, "quote_expired", str(error))
    if isinstance(error, (QuoteError, RateError)):
        return ApiError(400 if isinstance(error, QuoteError) else 503, "quote_error", str(error))
    if isinstance(error, LedgerError):
        return ApiError(400, "invalid_request", str(error))
    return None


def fields(body: dict[str, Any], required: dict[str, type], optional: dict[str, type] | None = None) -> dict[str, Any]:
    """Strict body validation: exact key set, exact types (bool is never accepted as int), bounded strings."""
    optional = optional or {}
    unknown = set(body) - set(required) - set(optional)
    missing = set(required) - set(body)
    if unknown or missing:
        raise ApiError(400, "invalid_request", "unexpected or missing fields: " + ", ".join(sorted(unknown | missing)))
    for name, value in body.items():
        wanted = required.get(name) or optional[name]
        if wanted is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ApiError(400, "invalid_request", f"{name} must be a whole number")
        if wanted is dict and not isinstance(value, dict):
            raise ApiError(400, "invalid_request", f"{name} must be an object")
        if wanted is str and (not isinstance(value, str) or len(value) > 256):
            raise ApiError(400, "invalid_request", f"{name} must be a string of at most 256 characters")
    return body


def query_int(query: dict[str, list[str]], name: str, default: int | None, low: int, high: int) -> int | None:
    values = query.get(name)
    if not values:
        return default
    try:
        number = int(values[0])
    except ValueError:
        raise ApiError(400, "invalid_request", f"{name} must be an integer") from None
    if not low <= number <= high:
        raise ApiError(400, "invalid_request", f"{name} must be between {low} and {high}")
    return number


def query_float(query: dict[str, list[str]], name: str, default: float) -> float:
    values = query.get(name)
    if not values:
        return default
    try:
        number = float(values[0])
    except ValueError:
        raise ApiError(400, "invalid_request", f"{name} must be a number") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ApiError(400, "invalid_request", f"{name} must be finite")
    return number


class Context:
    def __init__(self, service: PlatformService, ip: str, session: dict[str, Any] | None, body: dict[str, Any],
                 query: dict[str, list[str]], params: tuple[str, ...], key: str | None, token: str | None,
                 raw: bytes = b"", headers: Any = None) -> None:
        self.service, self.ip, self.session, self.body = service, ip, session, body
        self.raw, self.headers = raw, headers or {}
        self.query, self.params, self.key, self.token = query, params, key, token
        self.set_cookie: str | None = None
        self.clear_cookie = False

    @property
    def user(self) -> str:
        assert self.session is not None
        return self.session["user_id"]


class Api:
    """Route table and handlers; transport details live in :func:`make_handler`."""

    def __init__(self, service: PlatformService) -> None:
        self.service = service
        settings = service.settings
        self.secure = settings.secure_cookies
        self.ip_limiter = RateLimiter(600, 60)
        self.write_limiter = RateLimiter(120, 60)
        self.routes: list[tuple[str, re.Pattern[str], bool, bool, Callable[[Context], Any]]] = []
        add = self._add
        add("GET", "/api/health", False, False, lambda c: {"status": "ok", "simulation": c.service.settings.simulation_mode})
        add("POST", "/api/auth/register", False, False, self.register)
        add("POST", "/api/auth/login", False, False, self.login)
        add("POST", "/api/auth/logout", True, False, self.logout)
        add("POST", "/api/auth/logout-all", True, False, self.logout_all)
        add("POST", "/api/auth/change-password", True, False, self.change_password)
        add("POST", "/api/auth/password-reset/request", False, False, self.reset_request)
        add("POST", "/api/auth/password-reset/confirm", False, False, self.reset_confirm)
        add("GET", "/api/live/status", True, False, lambda c: c.service.live_status(c.user))
        add("POST", "/api/live/terms", True, False, self.live_terms)
        add("POST", "/api/live/deposits", True, True, self.live_deposit)
        add("POST", "/api/webhooks/payments", False, False, lambda c: c.service.handle_webhook("payments", c.headers, c.raw))
        add("POST", "/api/webhooks/chain", False, False, lambda c: c.service.handle_webhook("chain", c.headers, c.raw))
        add("GET", "/api/session", True, False, self.session_info)
        add("GET", "/api/security", True, False, lambda c: c.service.security(c.user))
        add("POST", "/api/security/email", True, False, self.set_email)
        add("POST", "/api/security/email/verify", True, False, self.verify_email)
        add("POST", "/api/security/pin/set", True, False, self.pin_set)
        add("POST", "/api/security/pin/change", True, False, self.pin_change)
        add("POST", "/api/security/pin/reset/request", True, False, self.pin_reset_request)
        add("POST", "/api/security/pin/reset/confirm", True, False, self.pin_reset_confirm)
        add("POST", "/api/security/totp/begin", True, False, self.totp_begin)
        add("POST", "/api/security/totp/confirm", True, False, self.totp_confirm)
        add("POST", "/api/security/totp/disable", True, False, self.totp_disable)
        add("POST", "/api/kyc/basic", True, False, self.kyc_basic)
        add("POST", "/api/kyc/enhanced", True, False, self.kyc_enhanced)
        add("POST", "/api/payouts/resolve", True, False, self.payout_resolve)
        add("GET", "/api/payouts/accounts", True, False, lambda c: {"accounts": c.service.payout_accounts(c.user)})
        add("POST", "/api/payouts/accounts", True, False, self.payout_add)
        add("POST", f"/api/payouts/accounts/{ID_RE}/delete", True, False, self.payout_delete)
        add("GET", "/api/wallet", True, False, lambda c: c.service.wallet(c.user))
        add("GET", "/api/wallet/history", True, False, self.history)
        add("POST", "/api/wallet/deposits", True, True, self.deposit)
        add("GET", "/api/withdrawals", True, False, lambda c: {"withdrawals": c.service.withdrawals(c.user)})
        add("POST", "/api/withdrawals", True, True, self.withdraw)
        add("POST", f"/api/withdrawals/{ID_RE}/cancel", True, False, lambda c: c.service.cancel_withdrawal(c.user, c.params[0]))
        add("POST", f"/api/withdrawals/{ID_RE}/simulate", True, False, self.simulate_payout)
        add("POST", "/api/quotes", True, False, self.quote)
        add("POST", f"/api/quotes/{ID_RE}/execute", True, True, self.execute_quote)
        add("GET", "/api/pools", True, False, lambda c: {"pools": c.service.pools()})
        add("GET", "/api/recommendation", True, False, self.recommendation)
        add("GET", f"/api/pools/{ID_RE}/scenario", True, False, self.scenario)
        add("GET", "/api/positions", True, False, lambda c: {"positions": c.service.list_positions(c.user)})
        add("POST", "/api/positions", True, True, self.open_position)
        add("POST", "/api/positions/deploy", True, True, self.deploy)
        add("POST", f"/api/positions/{ID_RE}/close", True, True, self.close_position)
        add("GET", "/api/activity", True, False, lambda c: {"events": c.service.activity(c.user, query_int(c.query, "limit", 30, 1, 100))})

    def _add(self, method: str, pattern: str, auth: bool, needs_key: bool, handler: Callable[[Context], Any]) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), auth, needs_key, handler))

    # ---- auth ---------------------------------------------------------------
    def register(self, c: Context) -> Any:
        body = fields(c.body, {"username": str, "password": str}, {"email": str})
        return c.service.accounts.register(body["username"], body["password"], c.ip, body.get("email")) | {"simulation": True}

    def login(self, c: Context) -> Any:
        body = fields(c.body, {"username": str, "password": str}, {"otp": str})
        session = c.service.accounts.login(body["username"], body["password"], c.ip, body.get("otp"))
        c.set_cookie = session.pop("token")
        return session | {"simulation": c.service.settings.simulation_mode}

    def logout(self, c: Context) -> Any:
        fields(c.body, {})
        c.service.accounts.logout(c.token or "")
        c.service.accounts.audit(c.user, "logout", c.ip)
        c.clear_cookie = True
        return {"status": "logged_out"}

    def logout_all(self, c: Context) -> Any:
        fields(c.body, {})
        c.service.accounts.logout_all(c.user)
        c.service.accounts.audit(c.user, "logout_all", c.ip)
        c.clear_cookie = True
        return {"status": "all_sessions_revoked"}

    def change_password(self, c: Context) -> Any:
        body = fields(c.body, {"current_password": str, "new_password": str})
        c.service.accounts.change_password(c.user, body["current_password"], body["new_password"],
                                           c.session["token_hash"], c.ip)
        return {"status": "password_changed", "other_sessions_revoked": True}

    def reset_request(self, c: Context) -> Any:
        body = fields(c.body, {"email": str})
        c.service.accounts.request_password_reset(body["email"], c.ip)
        return {"status": "if that email is verified on an account, a code was sent"}

    def reset_confirm(self, c: Context) -> Any:
        body = fields(c.body, {"email": str, "code": str, "new_password": str})
        c.service.accounts.reset_password(body["email"], body["code"], body["new_password"], c.ip)
        return {"status": "password_reset", "all_sessions_revoked": True}

    def set_email(self, c: Context) -> Any:
        c.service.accounts.set_email(c.user, fields(c.body, {"email": str})["email"], c.ip)
        return {"status": "verification code sent"}

    def verify_email(self, c: Context) -> Any:
        c.service.accounts.verify_email(c.user, fields(c.body, {"code": str})["code"], c.ip)
        return {"status": "email_verified"}

    def pin_set(self, c: Context) -> Any:
        body = fields(c.body, {"password": str, "pin": str})
        c.service.accounts.set_pin(c.user, body["password"], body["pin"], c.ip)
        return {"status": "pin_set"}

    def pin_change(self, c: Context) -> Any:
        body = fields(c.body, {"current_pin": str, "new_pin": str})
        c.service.accounts.change_pin(c.user, body["current_pin"], body["new_pin"], c.ip)
        return {"status": "pin_changed"}

    def pin_reset_request(self, c: Context) -> Any:
        fields(c.body, {})
        c.service.accounts.request_pin_reset(c.user, c.ip)
        return {"status": "a code was sent to your verified email"}

    def pin_reset_confirm(self, c: Context) -> Any:
        body = fields(c.body, {"code": str, "password": str, "new_pin": str})
        c.service.accounts.reset_pin(c.user, body["code"], body["password"], body["new_pin"], c.ip)
        return {"status": "pin_reset"}

    def live_terms(self, c: Context) -> Any:
        body = fields(c.body, {}, {"pin": str})
        return c.service.accept_live_terms(c.user, body.get("pin"), c.ip)

    def live_deposit(self, c: Context) -> Any:
        body = fields(c.body, {"amount_minor": int}, {"pin": str})
        return c.service.live_deposit_request(c.user, body["amount_minor"], c.key, body.get("pin"), c.ip)

    def execute_quote(self, c: Context) -> Any:
        body = fields(c.body, {}, {"pin": str})
        return c.service.execute_quote(c.user, c.params[0], c.key, body.get("pin"), c.ip)

    def close_position(self, c: Context) -> Any:
        body = fields(c.body, {}, {"pin": str})
        return c.service.close_position(c.user, c.params[0], c.key, body.get("pin"), c.ip)

    def totp_begin(self, c: Context) -> Any:
        fields(c.body, {})
        return c.service.accounts.totp_begin(c.user)

    def totp_confirm(self, c: Context) -> Any:
        c.service.accounts.totp_confirm(c.user, fields(c.body, {"code": str})["code"], c.ip)
        return {"status": "authenticator_enabled"}

    def totp_disable(self, c: Context) -> Any:
        body = fields(c.body, {"password": str, "code": str})
        c.service.accounts.totp_disable(c.user, body["password"], body["code"], c.ip)
        return {"status": "authenticator_disabled"}

    def kyc_basic(self, c: Context) -> Any:
        body = fields(c.body, {"full_name": str, "bvn": str})
        return c.service.kyc_basic(c.user, body["full_name"], body["bvn"])

    def kyc_enhanced(self, c: Context) -> Any:
        return c.service.kyc_enhanced(c.user, fields(c.body, {"address": str})["address"])

    def payout_resolve(self, c: Context) -> Any:
        return c.service.resolve_account(c.user, fields(c.body, {"account_number": str})["account_number"])

    def payout_add(self, c: Context) -> Any:
        body = fields(c.body, {"currency": str, "label": str, "destination": dict}, {"pin": str})
        self._small_destination(body["destination"])
        return c.service.add_payout_account(c.user, body["currency"], body["label"], body["destination"], body.get("pin"), c.ip)

    def payout_delete(self, c: Context) -> Any:
        body = fields(c.body, {}, {"pin": str})
        c.service.delete_payout_account(c.user, c.params[0], body.get("pin"), c.ip)
        return {"status": "deleted"}

    @staticmethod
    def _small_destination(dest: dict[str, Any]) -> None:
        if len(dest) > 4 or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 120 for k, v in dest.items()):
            raise ApiError(400, "invalid_request", "destination must hold a few short text fields")

    def session_info(self, c: Context) -> Any:
        return {"user": {"user_id": c.user, "username": c.session["username"]}, "csrf_token": c.session["csrf_token"],
                "expires_at": c.session["expires_at"], **c.service.session_info()}

    # ---- wallet -------------------------------------------------------------
    def history(self, c: Context) -> Any:
        limit = query_int(c.query, "limit", 50, 1, 200) or 50
        rows = c.service.history(c.user, limit, query_int(c.query, "before", None, 1, 10 ** 12))
        return {"entries": rows, "next_before": rows[-1]["entry_id"] if len(rows) == limit else None}

    def deposit(self, c: Context) -> Any:
        body = fields(c.body, {"currency": str, "amount_minor": int}, {"pin": str})
        return c.service.deposit(c.user, body["currency"], body["amount_minor"], c.key, body.get("pin"), c.ip)

    def withdraw(self, c: Context) -> Any:
        body = fields(c.body, {"currency": str, "amount_minor": int}, {"destination": dict, "beneficiary_id": str, "pin": str})
        if "destination" in body:
            self._small_destination(body["destination"])
        return c.service.request_withdrawal(c.user, body["currency"], body["amount_minor"], body.get("destination"), c.key,
                                            body.get("beneficiary_id"), body.get("pin"), c.ip)

    def simulate_payout(self, c: Context) -> Any:
        body = fields(c.body, {"outcome": str})
        return c.service.simulate_payout(c.user, c.params[0], body["outcome"])

    # ---- conversion, pools, positions ----------------------------------------
    def quote(self, c: Context) -> Any:
        body = fields(c.body, {"source_currency": str, "amount_minor": int})
        return c.service.create_quote(c.user, body["source_currency"], body["amount_minor"])

    def recommendation(self, c: Context) -> Any:
        return c.service.recommendation(query_int(c.query, "amount_minor", None, 1, 10 ** 13) or 0)

    def scenario(self, c: Context) -> Any:
        return c.service.scenario(c.params[0], query_float(c.query, "price_change_pct", -20.0),
                                  query_int(c.query, "days", 30, 1, 365) or 30)

    def open_position(self, c: Context) -> Any:
        body = fields(c.body, {"pool_id": str, "amount_minor": int}, {"pin": str})
        return c.service.open_position(c.user, body["pool_id"], body["amount_minor"], c.key, body.get("pin"), c.ip)

    def deploy(self, c: Context) -> Any:
        body = fields(c.body, {"amount_minor": int}, {"pin": str})
        return {"positions": c.service.deploy_recommendation(c.user, body["amount_minor"], c.key, body.get("pin"), c.ip)}


def make_handler(api: Api, allowed_hosts: frozenset[str]) -> type[BaseHTTPRequestHandler]:
    service, settings = api.service, api.service.settings
    public = settings.public_host.lower()
    allowed_hosts = frozenset(allowed_hosts | ({public} if public else set()))
    local = [h for h in allowed_hosts if h != public]
    allowed_origins = frozenset([f"http://{h}" for h in local] + ([f"https://{h}" for h in local] if api.secure else []) + ([f"{'https' if settings.public_https else 'http'}://{public}"] if public else []))
    secure_cookie = api.secure or settings.public_https
    static = {path: (name, ctype, (STATIC_DIR / name).read_bytes()) for path, (name, ctype) in STATIC.items()}

    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        protocol_version = "HTTP/1.0"          # one request per connection
        server_version = "platform"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:   # no access log: never write URLs/queries/bodies
            return

        # ---- response helpers
        def _send(self, status: int, payload: bytes, content_type: str, extra: dict[str, str] | None = None,
                  cookies: list[str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
            if settings.public_https:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            if getattr(self, "_mode_echo", None):
                self.send_header("X-Funds-Mode", self._mode_echo)
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            for cookie in cookies or []:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _json(self, status: int, data: Any, extra: dict[str, str] | None = None, cookies: list[str] | None = None) -> None:
            self._send(status, json.dumps(data, separators=(",", ":")).encode(), "application/json", extra, cookies)

        def _error(self, status: int, code: str, message: str, extra: dict[str, str] | None = None,
                   details: dict[str, Any] | None = None) -> None:
            self._json(status, {"error": {"code": code, "message": message, **(details or {})}}, extra)

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:   # type: ignore[override]
            try:
                self._error(code, "http_error", HTTPStatus(code).phrase)
            except (BrokenPipeError, ConnectionError):
                pass

        def _cookie(self, value: str, max_age: int) -> str:
            return f"session={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}" + ("; Secure" if secure_cookie else "")

        # ---- request checks
        def _client_ip(self) -> str:
            peer = self.client_address[0]
            if settings.trust_proxy and peer in ("127.0.0.1", "::1"):        # only a proxy on this machine is believed
                last = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
                try:
                    return str(ipaddress.ip_address(last))
                except ValueError:
                    return peer
            return peer

        def _check_host_and_origin(self, mutating: bool) -> None:
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                raise ApiError(400, "bad_host", "unexpected Host header")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                raise ApiError(403, "bad_origin", "cross-origin requests are not allowed")
            if mutating and self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                raise ApiError(403, "bad_origin", "cross-site requests are not allowed")

        def _read_body(self) -> dict[str, Any]:
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "invalid_request", "chunked bodies are not supported")
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                raise ApiError(415, "unsupported_media_type", "Content-Type must be application/json")
            length_text = self.headers.get("Content-Length")
            if length_text is None or not length_text.isdigit():
                raise ApiError(411, "length_required", "Content-Length is required")
            length = int(length_text)
            if length > MAX_BODY:
                raise ApiError(413, "too_large", "request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            self._raw = raw
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ApiError(400, "invalid_json", "body must be valid JSON") from None
            if not isinstance(body, dict):
                raise ApiError(400, "invalid_json", "body must be a JSON object")
            return body

        def _session(self) -> tuple[dict[str, Any] | None, str | None]:
            try:
                jar = SimpleCookie(self.headers.get("Cookie") or "")
            except CookieError:
                return None, None
            morsel = jar.get("session")
            token = morsel.value if morsel else None
            return (service.accounts.resolve(token) if token else None), token

        # ---- dispatch
        def _handle(self, method: str) -> None:
            request_id = secrets.token_hex(6)
            self._mode_echo = None
            try:
                url = urlsplit(self.path)
                if not self.path.startswith("/") or "\\" in self.path or "\x00" in self.path:
                    raise ApiError(400, "invalid_request", "bad request path")
                mutating = method in ("POST", "PUT", "PATCH", "DELETE")
                self._check_host_and_origin(mutating)
                api.ip_limiter.check_and_hit(f"ip:{self._client_ip()}")
                if method == "GET" and url.path in static:
                    _, ctype, payload = static[url.path]
                    return self._send(200, payload, ctype)
                if not url.path.startswith("/api/"):
                    raise ApiError(404, "not_found", "resource not found")
                matched, other_method = None, False
                for m, pattern, auth, needs_key, handler in api.routes:
                    found = pattern.match(url.path)
                    if found and m == method:
                        matched = (pattern, auth, needs_key, handler, found.groups())
                        break
                    other_method = other_method or bool(found)
                if matched is None:
                    raise ApiError(405 if other_method else 404, "method_not_allowed" if other_method else "not_found",
                                   "method not allowed" if other_method else "resource not found")
                _, auth, needs_key, handler, params = matched
                body = self._read_body() if mutating else {}
                session, token = self._session()
                if auth and session is None:
                    raise ApiError(401, "session_required", "authentication required")
                key = None
                if mutating and auth:
                    sent = self.headers.get("X-CSRF-Token") or ""
                    if not hmac.compare_digest(sent.encode(), session["csrf_token"].encode()):
                        raise ApiError(403, "csrf", "missing or invalid CSRF token")
                    api.write_limiter.check_and_hit(f"user:{session['user_id']}")
                if needs_key:
                    key = self.headers.get("Idempotency-Key") or ""
                    if not KEY_RE.fullmatch(key):
                        raise ApiError(400, "invalid_request", "Idempotency-Key header is required (8-100 characters)")
                ctx = Context(service, self._client_ip(), session, body, parse_qs(url.query, max_num_fields=20), params, key, token,
                              getattr(self, "_raw", b""), self.headers)
                mode = self.headers.get("X-Funds-Mode") or "demo"
                if session is not None and url.path.startswith(MODAL_PREFIXES):
                    with service.use_mode(mode, session["user_id"]):
                        self._mode_echo = mode
                        result = handler(ctx)
                else:
                    if mode not in ("demo", "live"):
                        raise ApiError(400, "invalid_request", "X-Funds-Mode must be 'demo' or 'live'")
                    result = handler(ctx)
                cookies = []
                if ctx.set_cookie:
                    cookies.append(self._cookie(ctx.set_cookie, settings.session_ttl_seconds))
                if ctx.clear_cookie:
                    cookies.append(self._cookie("", 0))
                status = 201 if method == "POST" and url.path in ("/api/auth/register", "/api/positions", "/api/withdrawals", "/api/quotes") else 200
                self._json(status, result, cookies=cookies)
            except ApiError as error:
                self._error(error.status, error.code, error.message, error.headers, error.details)
            except Exception as error:      # noqa: BLE001 - translate or hide everything
                mapped = translate(error)
                if mapped is not None:
                    return self._error(mapped.status, mapped.code, mapped.message, mapped.headers, mapped.details)
                log.error("request %s failed: %s", request_id, type(error).__name__, exc_info=True)
                self._error(500, "internal_error", f"internal error (reference {request_id})")

        def do_GET(self) -> None: self._handle("GET")
        def do_POST(self) -> None: self._handle("POST")
        def do_PUT(self) -> None: self._handle("PUT")
        def do_PATCH(self) -> None: self._handle("PATCH")
        def do_DELETE(self) -> None: self._handle("DELETE")
        def do_OPTIONS(self) -> None: self._handle("OPTIONS")
        def do_HEAD(self) -> None: self._handle("HEAD")

    return Handler


def create_server(service: PlatformService) -> ThreadingHTTPServer:
    settings = service.settings
    server = ThreadingHTTPServer((settings.host, settings.port), BaseHTTPRequestHandler)
    port = server.server_port
    hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})
    api = Api(service)
    server.api = api                     # exposed for tests and monitoring
    server.RequestHandlerClass = make_handler(api, hosts)
    server.daemon_threads = True
    return server
