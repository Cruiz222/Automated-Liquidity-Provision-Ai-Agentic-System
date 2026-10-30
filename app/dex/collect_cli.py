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
        help="live uses DefiLlama; fixture uses synthetic example data.",
    )
    parser.add_argument(
        "--database",
        default="data/dex_snapshots.sqlite3",
        help="Research database path (default: data/dex_snapshots.sqlite3).",
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
    parser.add_argument("--min-tvl", type=float, default=5_000_000.0)
    parser.add_argument("--max-pools", type=int, default=40)
    parser.add_argument("--history-days", type=int, default=30)

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


def build_provider(args: argparse.Namespace) -> Any:
    if args.mode == "fixture":
        return FixturePoolProvider(args.fixture_file)

    defaults = PoolUniverse()

    chains = tuple(x.strip() for x in args.chains.split(",") if x.strip())
    projects = tuple(x.strip() for x in args.projects.split(",") if x.strip())

    universe = PoolUniverse(
        chains=chains or defaults.chains,
        projects=projects or defaults.projects,
        tokens=defaults.tokens,
        min_tvl_usd=args.min_tvl,
        max_pools=args.max_pools,
        history_days=args.history_days,
    )
    return DefiLlamaPoolProvider(universe)


def print_result(result: Any) -> None:
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True), flush=True)


def persist_result(db: sqlite3.Connection, result: Any) -> None:
    """Commit a completed collection cycle before reporting success."""
    db.commit()
    print_result(result)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

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
    if args.max_pools < 1:
        parser.error("--max-pools must be at least 1.")
    if args.history_days < 1:
        parser.error("--history-days must be at least 1.")
    if args.keep_per_pool < 1:
        parser.error("--keep-per-pool must be at least 1.")

    database_path = Path(args.database).expanduser()
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
        print(
            "Data warning: fixture mode contains synthetic observations."
            if args.mode == "fixture"
            else "Data source: public DefiLlama APIs; read-only collection.",
            flush=True,
        )

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
                    on_result=lambda result: persist_result(db, result),
                )
            except KeyboardInterrupt:
                print("\nCollection stopped by user.", flush=True)
            return 0

        result = collector.collect_once()
        persist_result(db, result)
        return 0 if result.ok else 1

    except Exception:
        logging.exception("Collector could not start or continue.")
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
