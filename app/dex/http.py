"""Outbound JSON fetching for market-data providers.

The platform only ever reads from a short allowlist of public data hosts over HTTPS. Redirects are refused (a
redirect could send the request somewhere else), responses are size-capped, and every failure becomes a
:class:`ProviderError` so callers can fail closed instead of trusting partial data.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({"yields.llama.fi",
                           "coins.llama.fi",
                           "api.coingecko.com",
                           "dlmm.datapi.meteora.ag",
                           })

MAX_BYTES = 80_000_000          # the full DefiLlama pool list is tens of megabytes


class ProviderError(RuntimeError):
    """A data source failed or returned something unusable."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise ProviderError("redirects are not followed")


def fetch_json(url: str, timeout: float = 20.0, max_bytes: int = MAX_BYTES, allowed_hosts: frozenset[str] = ALLOWED_HOSTS) -> Any:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in allowed_hosts or parts.username or parts.password or parts.port not in (None, 443):
        raise ProviderError(f"refusing to fetch {parts.hostname or url!r}: only HTTPS to approved data hosts is allowed")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "liquidity-lab/0.6"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
    except ProviderError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ProviderError(f"request to {parts.hostname} failed: {error}") from error
    if len(payload) > max_bytes:
        raise ProviderError(f"response from {parts.hostname} is larger than {max_bytes} bytes")
    try:
        return json.loads(payload)
    except ValueError as error:
        raise ProviderError(f"{parts.hostname} returned invalid JSON") from error
