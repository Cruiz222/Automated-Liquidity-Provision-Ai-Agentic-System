"""Tiny cookie-aware HTTP client for tests (http.client, so headers can be controlled exactly)."""
from __future__ import annotations

import http.client
import json
import threading
import uuid
from typing import Any

from app.api.server import create_server
from app.config import Settings
from app.service import PlatformService

PASSWORD = "correct-horse-battery"


class RunningServer:
    def __init__(self, **overrides: Any) -> None:
        self.settings = Settings(database=overrides.pop("database", ":memory:"), port=0, password_iterations=1000, **overrides)
        self.service = PlatformService(self.settings)
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
        self.server, self.cookie, self.csrf = server, None, None

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
                key: bool | str = False, csrf: bool = True, raw: bytes | None = None) -> tuple[int, Any, http.client.HTTPMessage]:
        h = {"Host": f"127.0.0.1:{self.server.port}"}
        data = raw
        if body is not None:
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
        return self

    def get(self, path: str, **kw: Any):
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, key: bool | str = False, **kw: Any):
        return self.request("POST", path, {} if body is None else body, key=key, **kw)
