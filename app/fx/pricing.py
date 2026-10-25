"""NGN/USDT rate sources and a guarded aggregator.

A rate is "NGN per 1 USDT". A :class:`RateQuote` carries two sides:

* ``ask``: what a customer pays in NGN to buy 1 USDT (used for NGN -> USDT);
* ``bid``: what a customer receives in NGN for selling 1 USDT (USDT -> NGN).

:class:`RateService` refuses to answer from stale data, requires a minimum
number of agreeing sources, and rejects sources that disagree too much. It
never invents a rate; callers get a :class:`RateError` instead.

No network source is bundled as a default: pick and verify the data providers
you are licensed to use, then wrap them with :class:`HttpJsonRateSource`.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol, Sequence


class RateError(Exception):
    """Base class for rate failures."""


class RateUnavailable(RateError):
    pass


class StaleRate(RateUnavailable):
    pass


class RateDeviation(RateUnavailable):
    pass


def _to_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise RateError(f"{field} must be a number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RateError(f"{field} must be a number") from None
    if not result.is_finite() or result <= 0:
        raise RateError(f"{field} must be a positive finite number")
    return result


@dataclass(frozen=True)
class RateQuote:
    bid: Decimal
    ask: Decimal
    as_of: float
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bid", _to_decimal(self.bid, "bid"))
        object.__setattr__(self, "ask", _to_decimal(self.ask, "ask"))
        if self.ask < self.bid:
            raise RateError("ask must not be below bid")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * Decimal(10_000)


class RateSource(Protocol):
    name: str

    def fetch(self) -> RateQuote: ...


def _two_sided(mid: Decimal, spread_bps: Decimal | int | str) -> tuple[Decimal, Decimal]:
    half = Decimal(str(spread_bps)) / Decimal(20_000)
    if half < 0 or half >= 1:
        raise RateError("spread_bps must be between 0 and 20000")
    return mid * (1 - half), mid * (1 + half)


class StaticRateSource:
    """A fixed mid rate. For tests, demos and manual override; stamps each fetch with the clock time."""

    def __init__(self, mid: Decimal | str | int, spread_bps: Decimal | int | str = 0,
                 name: str = "static", clock: Callable[[], float] = time.time) -> None:
        self.name = name
        self._mid = _to_decimal(mid, "mid")
        self._spread_bps = spread_bps
        self._clock = clock

    def fetch(self) -> RateQuote:
        bid, ask = _two_sided(self._mid, self._spread_bps)
        return RateQuote(bid, ask, self._clock(), self.name)


class CallableRateSource:
    """Wrap any function returning a mid rate or a ``(bid, ask)`` pair."""

    def __init__(self, name: str, fn: Callable[[], Any], spread_bps: Decimal | int | str = 0,
                 clock: Callable[[], float] = time.time) -> None:
        self.name = name
        self._fn = fn
        self._spread_bps = spread_bps
        self._clock = clock

    def fetch(self) -> RateQuote:
        value = self._fn()
        if isinstance(value, (tuple, list)) and len(value) == 2:
            bid, ask = _to_decimal(value[0], "bid"), _to_decimal(value[1], "ask")
        else:
            bid, ask = _two_sided(_to_decimal(value, "mid"), self._spread_bps)
        return RateQuote(bid, ask, self._clock(), self.name)


def json_path(path: str) -> Callable[[Any], Any]:
    """Build an extractor for a dotted path, e.g. ``"data.0.price"`` (numeric parts index lists)."""
    parts = path.split(".")

    def extract(payload: Any) -> Any:
        node = payload
        for part in parts:
            try:
                node = node[int(part)] if isinstance(node, list) else node[part]
            except (KeyError, IndexError, ValueError, TypeError):
                raise RateError(f"rate not found at path {path!r}") from None
        return node

    return extract


def default_fetch_json(url: str, timeout: float = 5.0) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "liquidity-lab/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https enforced by caller)
        return json.load(response)


class HttpJsonRateSource:
    """Fetch a JSON document over HTTPS and extract a mid rate from it.

    ``extract`` maps the parsed payload to a mid rate (see :func:`json_path`).
    ``fetch_json`` is injectable so tests and other transports need no network.
    """

    def __init__(self, name: str, url: str, extract: Callable[[Any], Any],
                 spread_bps: Decimal | int | str = 0,
                 fetch_json: Callable[..., Any] = default_fetch_json,
                 clock: Callable[[], float] = time.time, timeout: float = 5.0) -> None:
        if not url.startswith("https://"):
            raise ValueError("rate sources must use https")
        self.name = name
        self.url = url
        self._extract = extract
        self._spread_bps = spread_bps
        self._fetch_json = fetch_json
        self._clock = clock
        self._timeout = timeout

    def fetch(self) -> RateQuote:
        try:
            payload = self._fetch_json(self.url, self._timeout)
        except RateError:
            raise
        except Exception as error:  # network, TLS, JSON errors are all "source unavailable"
            raise RateUnavailable(f"{self.name}: {error}") from error
        bid, ask = _two_sided(_to_decimal(self._extract(payload), "mid"), self._spread_bps)
        return RateQuote(bid, ask, self._clock(), self.name)


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


class RateService:
    """Aggregate sources into one trustworthy quote, or raise."""

    def __init__(self, sources: Sequence[RateSource], max_age_seconds: float = 60.0,
                 max_deviation_bps: int = 150, min_sources: int = 1,
                 clock: Callable[[], float] = time.time) -> None:
        if not sources:
            raise ValueError("at least one rate source is required")
        if min_sources < 1 or min_sources > len(sources):
            raise ValueError("min_sources must be between 1 and the number of sources")
        if max_age_seconds <= 0 or max_deviation_bps < 0:
            raise ValueError("max_age_seconds must be positive and max_deviation_bps non-negative")
        self.sources = list(sources)
        self.max_age_seconds = max_age_seconds
        self.max_deviation_bps = max_deviation_bps
        self.min_sources = min_sources
        self._clock = clock

    def current(self) -> RateQuote:
        now = self._clock()
        usable: list[RateQuote] = []
        failures: list[str] = []
        for source in self.sources:
            try:
                quote = source.fetch()
            except RateError as error:
                failures.append(f"{source.name}: {error}")
                continue
            except Exception as error:  # a misbehaving source must not crash conversions
                failures.append(f"{source.name}: {error}")
                continue
            if now - quote.as_of > self.max_age_seconds:
                failures.append(f"{source.name}: stale ({now - quote.as_of:.0f}s old)")
                continue
            usable.append(quote)
        if len(usable) < self.min_sources:
            message = f"{len(usable)} usable rate source(s), need {self.min_sources}: " + "; ".join(failures)
            if failures and all("stale" in failure for failure in failures) and not usable:
                raise StaleRate(message)
            raise RateUnavailable(message)
        mids = [quote.mid for quote in usable]
        if len(mids) > 1:
            spread = (max(mids) - min(mids)) / min(mids) * Decimal(10_000)
            if spread > self.max_deviation_bps:
                raise RateDeviation(f"sources disagree by {spread:.0f} bps (limit {self.max_deviation_bps})")
        return RateQuote(
            bid=_median([quote.bid for quote in usable]),
            ask=_median([quote.ask for quote in usable]),
            as_of=min(quote.as_of for quote in usable),
            source="+".join(quote.source for quote in usable),
        )
