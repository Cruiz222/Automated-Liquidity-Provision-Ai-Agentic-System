"""RFC 6238 time-based one-time passwords (HMAC-SHA1, 6 digits, 30 s steps) using only the standard library."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

STEP = 30


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def _hotp(key: bytes, step: int, digits: int = 6) -> str:
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(value).zfill(digits)


def code_at(secret: str, step: int, digits: int = 6) -> str:
    return _hotp(_key(secret), step, digits)


def current_step(now: float, period: int = STEP) -> int:
    return int(now // period)


def verify(secret: str, code: str, now: float, last_step: int = 0, window: int = 1) -> int | None:
    """Return the matched time step, or None. Steps at or before ``last_step`` are refused (no code reuse)."""
    if not isinstance(code, str) or not (code.isdigit() and len(code) == 6):
        return None
    base, matched = current_step(now), None
    try:
        for offset in range(-window, window + 1):
            step = base + offset
            if step > last_step and hmac.compare_digest(code_at(secret, step), code):
                matched = step if matched is None else max(matched, step)
    except (binascii.Error, ValueError):        # a corrupt or non-base32 secret must fail closed, not crash login
        return None
    return matched


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    return f"otpauth://totp/{quote(issuer + ':' + account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
