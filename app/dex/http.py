"""Outbound JSON fetching for market-data providers.

Requests are restricted to approved HTTPS hosts. Redirects are refused,
responses are size-capped, and transient failures receive bounded retries.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({
    "yields.llama.fi",
    "coins.llama.fi",
    "api.coingecko.com",
    "dlmm.datapi.meteora.ag",
})

MAX_BYTES = 80_000_000
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class ProviderError(RuntimeError):
    """A data source failed or returned something unusable."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise ProviderError("redirects are not followed")


def _retry_after_seconds(
    error: urllib.error.HTTPError,
    *,
    now: Callable[[], datetime],
    maximum: float,
) -> float | None:
    """Parse Retry-After as seconds or an HTTP date."""
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None

    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - now()).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None

    return min(max(delay, 0.0), maximum)


def fetch_json(
    url: str,
    timeout: float = 20.0,
    max_bytes: int = MAX_BYTES,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
    *,
    max_retries: int = 2,
    backoff_seconds: float = 0.5,
    max_backoff_seconds: float = 8.0,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Any:
    """Fetch JSON from an approved host, retrying transient failures only.

    max_retries counts retries after the initial attempt. The default therefore
    permits at most three attempts.
    """
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in allowed_hosts
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ProviderError(
            f"refusing to fetch {parts.hostname or url!r}: "
            "only HTTPS to approved data hosts is allowed"
        )

    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")
    for name, value in (
        ("backoff_seconds", backoff_seconds),
        ("max_backoff_seconds", max_backoff_seconds),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be a finite non-negative number")

    sleep = sleep or time.sleep
    clock = clock or (lambda: datetime.now(timezone.utc))

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "liquidity-lab/0.6",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)

    payload: bytes | None = None

    for attempt in range(max_retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = response.read(max_bytes + 1)
            break

        except ProviderError:
            # Redirect refusal and other deliberate safety failures are final.
            raise

        except urllib.error.HTTPError as error:
            status = error.code
            retryable = status in RETRYABLE_HTTP_STATUSES

            if not retryable or attempt >= max_retries:
                error.close()
                if retryable:
                    raise ProviderError(
                        f"{parts.hostname} returned HTTP {status} "
                        f"after {attempt + 1} attempts"
                    ) from error
                raise ProviderError(
                    f"{parts.hostname} returned HTTP {status}"
                ) from error

            delay = _retry_after_seconds(
                error,
                now=clock,
                maximum=max_backoff_seconds,
            )
            error.close()

        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt >= max_retries:
                raise ProviderError(
                    f"request to {parts.hostname} failed "
                    f"after {attempt + 1} attempts: {error}"
                ) from error
            delay = None

        except ValueError as error:
            raise ProviderError(
                f"request to {parts.hostname} failed: {error}"
            ) from error

        if delay is None:
            delay = min(
                backoff_seconds * (2 ** attempt),
                max_backoff_seconds,
            )
        sleep(delay)

    if payload is None:
        raise ProviderError(f"request to {parts.hostname} failed without a response")

    if len(payload) > max_bytes:
        raise ProviderError(
            f"response from {parts.hostname} is larger than {max_bytes} bytes"
        )

    try:
        return json.loads(payload)
    except ValueError as error:
        raise ProviderError(f"{parts.hostname} returned invalid JSON") from error
