"""Interfaces to the outside world for live mode, and sandbox implementations that touch nothing.

Live mode moves money in two places the platform does not control:

* a **payment rail** (NGN): collects deposits (for example into a virtual bank account) and sends bank payouts;
* a **chain executor** (USDT and liquidity positions): holds keys, sends USDT, opens and closes on-chain positions.

Both report back through **signed webhooks**. Only sandbox implementations exist here: they record intent, never
contact a bank or a chain, and are labelled ``sandbox`` everywhere they surface. Writing a real adapter means
implementing these protocols for a licensed provider; until then live mode cannot move real money.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Mapping, Protocol


class WebhookError(Exception):
    """A webhook was unsigned, stale, malformed or not understood."""


class WebhookRejected(WebhookError):
    """The webhook was authentic but cannot be applied (unknown reference, amount mismatch, bad state)."""


class PaymentRail(Protocol):
    name: str
    sandbox: bool

    def deposit_instructions(self, user_id: str, reference: str, amount_minor: int) -> dict[str, Any]: ...
    def send_payout(self, withdrawal: dict[str, Any]) -> str: ...
    def verify_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> dict[str, Any]: ...


class ChainExecutor(Protocol):
    name: str
    sandbox: bool

    def send_usdt(self, withdrawal: dict[str, Any]) -> str: ...
    def open_position(self, pool: Any, amount_minor: int, reference: str) -> str: ...
    def close_position(self, external_ref: str) -> None: ...
    def verify_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> dict[str, Any]: ...


class SignedWebhooks:
    """HMAC-SHA256 over ``<timestamp>.<raw body>``, five-minute tolerance, constant-time comparison."""

    TOLERANCE = 300

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("webhook secret must be at least 32 bytes")
        self._secret = secret

    def sign(self, body: bytes, timestamp: int) -> str:
        return hmac.new(self._secret, str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()

    def verify_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> dict[str, Any]:
        lowered = {k.lower(): v for k, v in headers.items()}
        try:
            timestamp = int(lowered["x-timestamp"])
            signature = lowered["x-signature"]
        except (KeyError, ValueError):
            raise WebhookError("missing or malformed signature headers") from None
        if abs(now - timestamp) > self.TOLERANCE:
            raise WebhookError("webhook timestamp is outside the allowed window")
        if not hmac.compare_digest(self.sign(body, timestamp), signature):
            raise WebhookError("bad signature")
        try:
            event = json.loads(body)
        except ValueError:
            raise WebhookError("body is not JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
            raise WebhookError("event needs a string id and type")
        return event


class SandboxPaymentRail(SignedWebhooks):
    name, sandbox = "sandbox-payments", True

    def deposit_instructions(self, user_id: str, reference: str, amount_minor: int) -> dict[str, Any]:
        return {"reference": reference, "bank_name": "SANDBOX BANK", "account_number": "0000000000",
                "account_name": "SANDBOX COLLECTIONS", "amount_minor": amount_minor,
                "note": "Sandbox rail: no real bank is involved. Complete the deposit with python3 -m app.sandbox_webhook."}

    def send_payout(self, withdrawal: dict[str, Any]) -> str:
        return "sbx-pay-" + hashlib.sha256(withdrawal["withdrawal_id"].encode()).hexdigest()[:16]


class SandboxChainExecutor(SignedWebhooks):
    name, sandbox = "sandbox-chain", True

    def send_usdt(self, withdrawal: dict[str, Any]) -> str:
        return "0xsbx" + hashlib.sha256(withdrawal["withdrawal_id"].encode()).hexdigest()[:40]

    def open_position(self, pool: Any, amount_minor: int, reference: str) -> str:
        return "sbx-pos-" + hashlib.sha256(f"{pool.pool_id}:{reference}".encode()).hexdigest()[:16]

    def close_position(self, external_ref: str) -> None:
        if not external_ref.startswith("sbx-pos-"):
            raise WebhookError("not a sandbox position")


def build_adapters(kind: str, secret: str) -> tuple[PaymentRail | None, ChainExecutor | None]:
    """Map the ``live_rails`` setting to adapters. Only ``sandbox`` exists."""
    if not kind:
        return None, None
    if kind != "sandbox":
        raise ValueError(f"unknown live rails {kind!r}; only 'sandbox' is implemented")
    key = secret.encode()
    return SandboxPaymentRail(key), SandboxChainExecutor(key)


def sign_event(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    """Headers for a signed webhook (used by the sandbox CLI and tests)."""
    stamp = int(time.time()) if timestamp is None else timestamp
    return {"X-Timestamp": str(stamp), "X-Signature": SignedWebhooks(secret.encode()).sign(body, stamp)}
