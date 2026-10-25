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

from ..accounts import AccountError, AuthError, ConflictError, RateLimited, RateLimiter, ValidationError
from ..config import Settings
from ..fx import QuoteError, QuoteExpired, RateError
from ..ledger import (
    IdempotencyConflict, InsufficientFunds, InvalidOperation, InvalidTransition, LedgerError, LimitExceeded,
)
from ..portfolio import PositionNotFound
from ..service import NotFound, PlatformService, SimulationDisabled

log = logging.getLogger("app.api")
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/static/app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
MAX_BODY = 16 * 1024
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")
ID_RE = r"([A-Za-z0-9_.:-]{1,120})"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def translate(error: Exception) -> ApiError | None:
    """Map domain exceptions to HTTP errors. Unknown exceptions return None (generic 500)."""
    if isinstance(error, ApiError):
        return error
    if isinstance(error, RateLimited):
        return ApiError(429, "rate_limited", str(error), {"Retry-After": str(error.retry_after)})
    if isinstance(error, AuthError):
        return ApiError(401, "unauthorized", str(error))
    if isinstance(error, ConflictError):
        return ApiError(409, "conflict", str(error))
    if isinstance(error, (ValidationError, InvalidOperation)):
        return ApiError(400, "invalid_request", str(error))
    if isinstance(error, (NotFound, PositionNotFound)):
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
                 query: dict[str, list[str]], params: tuple[str, ...], key: str | None, token: str | None) -> None:
        self.service, self.ip, self.session, self.body = service, ip, session, body
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
        add("GET", "/api/session", True, False, self.session_info)
        add("GET", "/api/wallet", True, False, lambda c: c.service.wallet(c.user))
        add("GET", "/api/wallet/history", True, False, self.history)
        add("POST", "/api/wallet/deposits", True, True, self.deposit)
        add("GET", "/api/withdrawals", True, False, lambda c: {"withdrawals": c.service.withdrawals(c.user)})
        add("POST", "/api/withdrawals", True, True, self.withdraw)
        add("POST", f"/api/withdrawals/{ID_RE}/cancel", True, False, lambda c: c.service.cancel_withdrawal(c.user, c.params[0]))
        add("POST", f"/api/withdrawals/{ID_RE}/simulate", True, False, self.simulate_payout)
        add("POST", "/api/quotes", True, False, self.quote)
        add("POST", f"/api/quotes/{ID_RE}/execute", True, True, lambda c: c.service.execute_quote(c.user, c.params[0], c.key))
        add("GET", "/api/pools", True, False, lambda c: {"pools": c.service.pools()})
        add("GET", "/api/recommendation", True, False, self.recommendation)
        add("GET", f"/api/pools/{ID_RE}/scenario", True, False, self.scenario)
        add("GET", "/api/positions", True, False, lambda c: {"positions": c.service.list_positions(c.user)})
        add("POST", "/api/positions", True, True, self.open_position)
        add("POST", "/api/positions/deploy", True, True, self.deploy)
        add("POST", f"/api/positions/{ID_RE}/close", True, True, lambda c: c.service.close_position(c.user, c.params[0], c.key))
        add("GET", "/api/activity", True, False, lambda c: {"events": c.service.activity(c.user, query_int(c.query, "limit", 30, 1, 100))})

    def _add(self, method: str, pattern: str, auth: bool, needs_key: bool, handler: Callable[[Context], Any]) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), auth, needs_key, handler))

    # ---- auth ---------------------------------------------------------------
    def register(self, c: Context) -> Any:
        body = fields(c.body, {"username": str, "password": str})
        return c.service.accounts.register(body["username"], body["password"], c.ip) | {"simulation": True}

    def login(self, c: Context) -> Any:
        body = fields(c.body, {"username": str, "password": str})
        session = c.service.accounts.login(body["username"], body["password"], c.ip)
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

    def session_info(self, c: Context) -> Any:
        return {"user": {"user_id": c.user, "username": c.session["username"]}, "csrf_token": c.session["csrf_token"],
                "expires_at": c.session["expires_at"], **c.service.session_info()}

    # ---- wallet -------------------------------------------------------------
    def history(self, c: Context) -> Any:
        limit = query_int(c.query, "limit", 50, 1, 200) or 50
        rows = c.service.history(c.user, limit, query_int(c.query, "before", None, 1, 10 ** 12))
        return {"entries": rows, "next_before": rows[-1]["entry_id"] if len(rows) == limit else None}

    def deposit(self, c: Context) -> Any:
        body = fields(c.body, {"currency": str, "amount_minor": int})
        return c.service.deposit(c.user, body["currency"], body["amount_minor"], c.key)

    def withdraw(self, c: Context) -> Any:
        body = fields(c.body, {"currency": str, "amount_minor": int, "destination": dict})
        dest = body["destination"]
        if len(dest) > 4 or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 120 for k, v in dest.items()):
            raise ApiError(400, "invalid_request", "destination must hold a few short text fields")
        return c.service.request_withdrawal(c.user, body["currency"], body["amount_minor"], dest, c.key)

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
        body = fields(c.body, {"pool_id": str, "amount_minor": int})
        return c.service.open_position(c.user, body["pool_id"], body["amount_minor"], c.key)

    def deploy(self, c: Context) -> Any:
        body = fields(c.body, {"amount_minor": int})
        return {"positions": c.service.deploy_recommendation(c.user, body["amount_minor"], c.key)}


def make_handler(api: Api, allowed_hosts: frozenset[str]) -> type[BaseHTTPRequestHandler]:
    service, settings = api.service, api.service.settings
    allowed_origins = frozenset(f"{'https' if api.secure else 'http'}://{h}" for h in allowed_hosts)
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
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            for cookie in cookies or []:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _json(self, status: int, data: Any, extra: dict[str, str] | None = None, cookies: list[str] | None = None) -> None:
            self._send(status, json.dumps(data, separators=(",", ":")).encode(), "application/json", extra, cookies)

        def _error(self, status: int, code: str, message: str, extra: dict[str, str] | None = None) -> None:
            self._json(status, {"error": {"code": code, "message": message}}, extra)

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:   # type: ignore[override]
            try:
                self._error(code, "http_error", HTTPStatus(code).phrase)
            except (BrokenPipeError, ConnectionError):
                pass

        def _cookie(self, value: str, max_age: int) -> str:
            return f"session={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}" + ("; Secure" if api.secure else "")

        # ---- request checks
        def _client_ip(self) -> str:
            return self.client_address[0]

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
                    raise ApiError(401, "unauthorized", "authentication required")
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
                ctx = Context(service, self._client_ip(), session, body, parse_qs(url.query, max_num_fields=20), params, key, token)
                result = handler(ctx)
                cookies = []
                if ctx.set_cookie:
                    cookies.append(self._cookie(ctx.set_cookie, settings.session_ttl_seconds))
                if ctx.clear_cookie:
                    cookies.append(self._cookie("", 0))
                status = 201 if method == "POST" and url.path in ("/api/auth/register", "/api/positions", "/api/withdrawals", "/api/quotes") else 200
                self._json(status, result, cookies=cookies)
            except ApiError as error:
                self._error(error.status, error.code, error.message, error.headers)
            except Exception as error:      # noqa: BLE001 - translate or hide everything
                mapped = translate(error)
                if mapped is not None:
                    return self._error(mapped.status, mapped.code, mapped.message, mapped.headers)
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
