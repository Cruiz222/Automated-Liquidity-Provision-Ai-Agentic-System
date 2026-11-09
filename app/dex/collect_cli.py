"""Collect DEX pool observations into a standalone SQLite research database.

Examples:
    python3 -m app.dex.collect_cli --mode live
    python3 -m app.dex.collect_cli --mode live --watch --interval 300
    python3 -m app.dex.collect_cli --mode fixture
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .collector import PoolSnapshotCollector
from .defillama import DefiLlamaPoolProvider, PoolUniverse
from .meteora import MeteoraDLMMProvider, MeteoraUniverse
from .provider import FixturePoolProvider
from .snapshot_store import SnapshotStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect DEX pool snapshots into a separate SQLite database."
    )

    parser.add_argument(
        "--mode",
        choices=("live", "fixture"),
        required=True,
        help="live uses a selected public provider; fixture uses synthetic example data.",
    )
    parser.add_argument(
        "--provider",
        choices=("defillama", "meteora"),
        default="defillama",
        help="Live data provider (default: defillama).",
    )
    parser.add_argument(
        "--database",
        default=None,
        help=(
            "Research database path. If omitted, a separate default database "
            "is selected for the provider or fixture mode."
        ),
    )
    parser.add_argument(
        "--fixture-file",
        default=str(
            Path(__file__).resolve().parents[2]
            / "examples"
            / "pools_snapshot.json"
        ),
        help="Fixture JSON file used when --mode fixture.",
    )

    parser.add_argument(
        "--chains",
        default="",
        help="Comma-separated live-data chains. Empty uses provider defaults.",
    )
    parser.add_argument(
        "--projects",
        default="",
        help="Comma-separated live-data projects. Empty uses provider defaults.",
    )
    parser.add_argument(
        "--min-tvl",
        type=float,
        default=None,
        help=(
            "Minimum pool TVL in USD. Defaults to $5,000,000 for DefiLlama "
            "and $100,000 for Meteora."
        ),
    )
    parser.add_argument("--max-pools", type=int, default=40)
    parser.add_argument(
        "--page-size", type=int, default=100,
        help="Meteora API page size, from 1 to 1000 (default: 100).",
    )
    parser.add_argument(
        "--max-pages", type=int, default=10,
        help="Maximum Meteora pages to inspect (default: 10).",
    )
    parser.add_argument(
        "--include-blacklisted", action="store_true",
        help="Include Meteora pools marked as blacklisted (not recommended).",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=30,
        help="History span in days (30 days means 720 points for hourly charts).",
    )
    parser.add_argument(
        "--history-period",
        choices=("1d", "1h"),
        default="1d",
        help="DefiLlama chart resolution (default: 1d).",
    )
    parser.add_argument(
        "--history-points",
        type=int,
        default=None,
        help="Explicit chart span in observations; for example 720 with --history-period 1h.",
    )

    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep collecting repeatedly instead of collecting once.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help="Seconds between collection cycles in watch mode (default: 300).",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Stop watch mode after this many cycles. Omit to run until Ctrl+C.",
    )
    parser.add_argument(
        "--keep-per-pool",
        type=int,
        default=200,
        help="Maximum snapshots retained per pool. Use 200 by default.",
    )

    return parser


def resolve_database_path(args: argparse.Namespace) -> Path:
    """Resolve an explicit database path or the mode/provider default."""
    if args.database is not None:
        return Path(args.database).expanduser()

    if args.mode == "fixture":
        return Path("data/fixture_snapshots.sqlite3")

    if args.provider == "meteora":
        return Path("data/meteora_snapshots.sqlite3")

    return Path("data/defillama_snapshots.sqlite3")


def resolve_min_tvl(args: argparse.Namespace) -> float:
    """Resolve an explicit TVL threshold or the provider-specific default."""
    if args.min_tvl is not None:
        return args.min_tvl

    if args.mode == "live" and args.provider == "meteora":
        return 100_000.0

    return 5_000_000.0


def build_provider(args: argparse.Namespace) -> Any:
    min_tvl = resolve_min_tvl(args)

    if args.mode == "fixture":
        if args.provider != "defillama":
            raise ValueError("--provider can only be used with --mode live.")
        return FixturePoolProvider(args.fixture_file)

    if args.provider == "meteora":
        universe = MeteoraUniverse(
            min_tvl_usd=min_tvl,
            max_pools=args.max_pools,
            page_size=args.page_size,
            max_pages=args.max_pages,
            include_blacklisted=args.include_blacklisted,
        )
        return MeteoraDLMMProvider(universe)

    defaults = PoolUniverse()

    chains = tuple(x.strip() for x in args.chains.split(",") if x.strip())
    projects = tuple(x.strip() for x in args.projects.split(",") if x.strip())

    universe = PoolUniverse(
        chains=chains or defaults.chains,
        projects=projects or defaults.projects,
        tokens=defaults.tokens,
        min_tvl_usd=min_tvl,
        max_pools=args.max_pools,
        history_days=args.history_days,
        history_period=args.history_period,
        history_points=args.history_points,
    )
    return DefiLlamaPoolProvider(universe)


def print_result(result: Any) -> None:
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True), flush=True)


def persist_result(db: sqlite3.Connection, result: Any, provider: Any = None) -> None:
    """Commit a completed collection cycle before reporting results."""
    db.commit()
    print_result(result)

    diagnostics = getattr(provider, "last_report", None)
    if diagnostics:
        print("Provider diagnostics:", flush=True)
        print(json.dumps(diagnostics, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.min_tvl = resolve_min_tvl(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.watch and args.interval <= 0:
        parser.error("--interval must be greater than zero.")
    if args.watch and args.cycles is not None and args.cycles < 1:
        parser.error("--cycles must be at least 1.")
    if not args.watch and args.cycles is not None:
        parser.error("--cycles can only be used together with --watch.")
    if args.min_tvl < 0:
        parser.error("--min-tvl cannot be negative.")
    if args.mode == "fixture" and args.provider != "defillama":
        parser.error("--provider can only be used with --mode live.")
    if args.mode == "live" and args.provider == "meteora" and args.min_tvl <= 0:
        parser.error("--min-tvl must be greater than zero for Meteora.")
    if args.page_size < 1 or args.page_size > 1000:
        parser.error("--page-size must be between 1 and 1000.")
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1.")
    if args.max_pools < 1:
        parser.error("--max-pools must be at least 1.")
    if args.history_days < 1:
        parser.error("--history-days must be at least 1.")
    if args.history_points is not None and args.history_points < 3:
        parser.error("--history-points must be at least 3.")
    if args.history_period == "1h" and args.history_points is None:
        parser.error("--history-points is required with --history-period 1h.")
    if args.history_period == "1d" and args.history_points is not None:
        parser.error("--history-points is only supported with --history-period 1h.")
    if args.keep_per_pool < 1:
        parser.error("--keep-per-pool must be at least 1.")

    database_path = resolve_database_path(args)
    if str(database_path) == ":memory:":
        parser.error("Use a file path for --database; the collector is intended to persist observations.")

    database_path.parent.mkdir(parents=True, exist_ok=True)

    db: sqlite3.Connection | None = None
    try:
        db = sqlite3.connect(
            str(database_path),
            timeout=30,
            check_same_thread=False,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA journal_mode = WAL")

        lock = threading.RLock()
        store = SnapshotStore(
            db,
            lock,
            keep_per_pool=args.keep_per_pool,
        )

        provider = build_provider(args)
        collector = PoolSnapshotCollector(provider, store)

        print(f"Mode: {args.mode}", flush=True)
        print(f"Research database: {database_path}", flush=True)
        if args.mode == "live" and args.provider == "defillama":
            universe = provider.universe
            print(
                f"Price history: period={universe.history_period}, "
                f"span={universe.effective_history_span} observations, "
                f"alignment_tolerance={universe.effective_alignment_tolerance_seconds}s",
                flush=True,
            )
        elif args.mode == "live" and args.provider == "meteora":
            universe = provider.universe
            print(
                "Meteora discovery: "
                f"min_tvl_usd={universe.min_tvl_usd}, "
                f"max_pools={universe.max_pools}, "
                f"page_size={universe.page_size}, "
                f"max_pages={universe.max_pages}, "
                f"include_blacklisted={universe.include_blacklisted}",
                flush=True,
            )

        if args.mode == "fixture":
            print("Data warning: fixture mode contains synthetic observations.", flush=True)
        elif args.provider == "meteora":
            print("Data source: public Meteora DLMM API; read-only collection.", flush=True)
        else:
            print("Data source: public DefiLlama APIs; read-only collection.", flush=True)

        if args.watch:
            print(
                f"Continuous collection enabled; interval={args.interval}s; "
                f"cycles={args.cycles or 'until interrupted'}",
                flush=True,
            )
            try:
                collector.collect_forever(
                    interval_seconds=args.interval,
                    max_cycles=args.cycles,
                    on_result=lambda result: persist_result(db, result, provider),
                )
            except KeyboardInterrupt:
                print("\nCollection stopped by user.", flush=True)
            return 0

        result = collector.collect_once()
        persist_result(db, result, provider)
        return 0 if result.ok else 1

    except Exception:
        logging.exception("Collector could not start or continue.")
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
