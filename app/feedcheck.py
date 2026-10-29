"""Check the live data feeds from a machine with internet access:

    python3 -m app.feedcheck [--rates coingecko-tether-ngn] [--chains ethereum,arbitrum] [--projects uniswap-v3,...]

Fetches the pool list, prices and history, parses them with the same code the platform uses, scores the pools and
prints what survived each stage. Nothing is written anywhere. If a count is zero, the printed project list shows what
the feed actually contains so you can adjust the universe (DEX_POOL_CHAINS, DEX_POOL_PROJECTS).
"""
from __future__ import annotations

import argparse
import time
from typing import Any, Callable

from .dex import PoolSelector
from .dex.defillama import DefiLlamaPoolProvider, PoolUniverse
from .dex.http import ProviderError
from .fx import build_rate_service


def run_check(provider: Any, rates: Any = None, out: Callable[[str], None] = print) -> int:
    started = time.time()
    try:
        snapshots = provider.fetch_pools()
    except ProviderError as error:
        out(f"FAILED: pool data could not be loaded: {error}")
        return 1
    report = getattr(provider, "last_report", {})
    out(f"pool data loaded in {time.time() - started:.1f}s")
    for key in ("rows", "after_filter", "snapshots", "with_usd_prices", "with_price_history"):
        if key in report:
            out(f"  {key.replace('_', ' '):<22}{report[key]}")
    if report.get("projects_on_chosen_chains"):
        out("  projects on the chosen chains: " + ", ".join(f"{k} ({v})" for k, v in report["projects_on_chosen_chains"].items()))
    selector = PoolSelector()
    selector.add_pools(snapshots)
    evaluations = sorted((selector.evaluate(s, time.time()) for s in snapshots), key=lambda e: (not e.eligible, -e.score))
    out(f"\n{sum(e.eligible for e in evaluations)} of {len(evaluations)} pools are eligible under the default policy:")
    for e in evaluations[:12]:
        note = "; ".join(e.reasons[:1] or e.warnings[:1])
        out(f"  {'+' if e.eligible else '-'} {'/'.join(e.tokens):<14}{e.chain:<10}tvl ${e.tvl_usd / 1e6:>7.1f}M  fee {e.fee_apr:>6.1%}  "
            f"IL {e.il_apr:>5.1%}  net {e.net_apr:>6.1%}  risk {e.risk_score:.2f}  {note}")
    status = 0 if report.get("with_usd_prices", 1) else 2
    if rates is not None:
        out("\nrate sources:")
        try:
            quote = rates.current()
            out(f"  OK mid {quote.mid:.2f} NGN per USDT (bid {quote.bid:.2f}, ask {quote.ask:.2f}) from {quote.source}")
        except Exception as error:      # noqa: BLE001 - report any source failure
            out(f"  FAILED: {error}")
            status = status or 3
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chains", default="")
    parser.add_argument("--projects", default="")
    parser.add_argument("--rates", default="", help="comma-separated rate source names (e.g. coingecko-tether-ngn)")
    args = parser.parse_args()
    defaults = PoolUniverse()
    universe = PoolUniverse(chains=tuple(x for x in args.chains.split(",") if x) or defaults.chains,
                            projects=tuple(x for x in args.projects.split(",") if x) or defaults.projects)
    rates = build_rate_service([x for x in args.rates.split(",") if x]) if args.rates else None
    raise SystemExit(run_check(DefiLlamaPoolProvider(universe), rates))


if __name__ == "__main__":
    main()
