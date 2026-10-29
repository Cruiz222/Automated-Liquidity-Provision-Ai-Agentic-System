"""Ready-made NGN/USDT reference-rate sources and a builder that turns settings into a guarded RateService.

A reference rate is not an executable price: Nigerian over-the-counter and peer-to-peer rates differ from it, and a
real conversion needs a counterparty's firm quote. Live conversions therefore require at least two agreeing sources.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from ..dex.http import fetch_json
from .pricing import HttpJsonRateSource, RateService, json_path

# name -> (url, dotted path to the NGN-per-USDT number). Host must be on dex.http.ALLOWED_HOSTS.
PRESETS = {"coingecko-tether-ngn": ("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=ngn", "tether.ngn")}


def build_rate_service(names: Sequence[str], spread_bps: int = 100, min_sources: int = 1,
                       fetch: Callable[..., Any] = fetch_json) -> RateService:
    """``names`` are preset names or ``url|dotted.path`` pairs. Raises ValueError for anything unknown."""
    if not names:
        raise ValueError("at least one rate source is required")
    sources = []
    for name in names:
        if name in PRESETS:
            url, path = PRESETS[name]
        elif "|" in name:
            url, path = name.split("|", 1)
        else:
            raise ValueError(f"unknown rate source {name!r}")
        sources.append(HttpJsonRateSource(name.split("|")[0][:60], url, json_path(path), spread_bps=spread_bps,
                                          fetch_json=lambda u, timeout=5.0, _f=fetch: _f(u, timeout)))
    return RateService(sources, max_age_seconds=120, max_deviation_bps=150, min_sources=min(min_sources, len(sources)))
