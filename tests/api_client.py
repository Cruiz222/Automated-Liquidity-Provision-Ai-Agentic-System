"""Tiny cookie-aware HTTP client for tests (http.client, so headers can be controlled exactly)."""
from __future__ import annotations

import http.client
import json
import re
import threading
import uuid
from typing import Any

from app.api.server import create_server
from app.config import Settings
from app.service import PlatformService

PASSWORD = "correct-horse-battery"
PIN = "482915"
MONEY_PATHS = re.compile(r"^/api/(wallet/deposits|live/deposits|withdrawals|positions|positions/deploy|payouts/accounts|"
                         r"quotes/[\w.:-]+/execute|positions/[\w.:-]+/close|payouts/accounts/[\w.:-]+/delete)$")


class RunningServer:
    def __init__(self, service_kwargs: dict[str, Any] | None = None, **overrides: Any) -> None:
        service_kwargs = service_kwargs or {}
        self.settings = Settings(database=overrides.pop("database", ":memory:"), port=0, password_iterations=1000, **overrides)
        self.service = PlatformService(self.settings, **service_kwargs)
        self.server = create_server(self.service)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.service.close()


class Client:
    def __init__(self, server: RunningServer) -> None:
        self.server, self.cookie, self.csrf, self.pin = server, None, None, None

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
                key: bool | str = False, csrf: bool = True, raw: bytes | None = None, pin: bool = True) -> tuple[int, Any, http.client.HTTPMessage]:
        h = {"Host": f"127.0.0.1:{self.server.port}"}
        data = raw
        if body is not None:
            if pin and method == "POST" and self.pin and isinstance(body, dict) and "pin" not in body and MONEY_PATHS.match(path):
                body = {**body, "pin": self.pin}
            data = json.dumps(body).encode()
        if data is not None or method == "POST":
            h["Content-Type"] = "application/json"
        if self.cookie:
            h["Cookie"] = f"session={self.cookie}"
        if csrf and self.csrf and method != "GET":
            h["X-CSRF-Token"] = self.csrf
        if key:
            h["Idempotency-Key"] = key if isinstance(key, str) else uuid.uuid4().hex
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            conn.request(method, path, body=data if data is not None else (b"{}" if method == "POST" else None), headers=h)
            response = conn.getresponse()
            payload = response.read()
            parsed = json.loads(payload) if payload and "json" in (response.getheader("Content-Type") or "") else payload
            return response.status, parsed, response.msg
        finally:
            conn.close()

    def register_login(self, username: str, password: str = PASSWORD) -> "Client":
        assert self.request("POST", "/api/auth/register", {"username": username, "password": password})[0] == 201
        status, body, headers = self.request("POST", "/api/auth/login", {"username": username, "password": password})
        assert status == 200, body
        self.cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.csrf = body["csrf_token"]
        self.user_id = body["user"]["user_id"]
        status, result, _ = self.post("/api/security/pin/set", {"password": password, "pin": PIN})
        assert status == 200, result
        self.pin = PIN
        return self

    _bvn = iter(range(10 ** 10, 10 ** 10 + 10000))

    def verify_kyc(self) -> "Client":
        status, body, _ = self.post("/api/kyc/basic", {"full_name": "Test Customer", "bvn": str(next(Client._bvn))})
        assert status == 200, body
        return self

    def get(self, path: str, **kw: Any):
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, key: bool | str = False, **kw: Any):
        return self.request("POST", path, {} if body is None else body, key=key, **kw)
