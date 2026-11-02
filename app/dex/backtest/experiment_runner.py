"""Reproducible DEX liquidity-provision OOS experiment runner.

Experiments consume an immutable SQLite snapshot database and produce
integrity-checked JSON artifacts in the experiment output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import threading
from typing import Any

from app.dex.backtest.oos import DEXOutOfSampleEvaluator
from app.dex.snapshot_store import SnapshotStore
from app.experiments import Registry, read_runs, write_artifact


IMPLEMENTATION_FILES = (
    "app/dex/backtest/experiment_runner.py",
    "app/dex/backtest/oos.py",
    "app/dex/backtest/market_data.py",
    "app/dex/backtest/portfolio.py",
    "app/dex/backtest/accounting.py",
    "app/dex/backtest/splitting.py",
    "app/dex/selector.py",
    "app/dex/pool_data.py",
    "app/dex/snapshot_store.py",
    "app/dex/amm.py",
)

DEFAULT_OUTPUT_DIR = Path("data/dex_experiments")
MANIFEST_VERSION = 1


class DEXExperimentError(ValueError):
    """Invalid DEX experiment input or artifact state."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_hash(root: Path | None = None) -> str:
    """Hash the DEX implementation files, independent of current directory."""
    repository_root = (
        root.resolve()
        if root is not None
        else Path(__file__).resolve().parents[3]
    )

    file_hashes: dict[str, str] = {}

    for relative_name in IMPLEMENTATION_FILES:
        path = repository_root / relative_name
        if not path.is_file():
            raise DEXExperimentError(
                f"Implementation file is missing: {relative_name}"
            )
        file_hashes[relative_name] = _sha256_file(path)

    return _sha256_bytes(_canonical_json(file_hashes).encode("utf-8"))


def _validate_number(name: str, value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise DEXExperimentError(
            f"{name} must be a non-negative finite number"
        )
    return float(value)


def _validate_capital(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        raise DEXExperimentError(
            "capital_micro_usdt must be a positive integer"
        )
    return value


def _validate_snapshot_database(path: Path) -> dict[str, Any]:
    """Validate a snapshot database without creating or modifying anything."""
    if not path.is_file():
        raise DEXExperimentError(
            f"Snapshot database does not exist: {path}"
        )

    # A WAL database may have committed state outside the main file.
    # Reject sidecars so the input hash identifies the evaluated snapshot.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            raise DEXExperimentError(
                f"Snapshot database has a SQLite {suffix} sidecar; "
                "checkpoint and close it before running the experiment"
            )

    uri = path.resolve().as_uri() + "?mode=ro"

    try:
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise DEXExperimentError(
                    f"Snapshot database integrity check failed: {integrity}"
                )

            tables = {
                row["name"]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "pool_snapshots" not in tables:
                raise DEXExperimentError(
                    "Snapshot database is missing pool_snapshots table"
                )

            columns = {
                row["name"]
                for row in db.execute(
                    "PRAGMA table_info(pool_snapshots)"
                )
            }
            required = {
                "snapshot_id",
                "pool_id",
                "as_of",
                "source",
                "payload_json",
            }
            if not required.issubset(columns):
                missing = sorted(required - columns)
                raise DEXExperimentError(
                    f"Snapshot table is missing columns: {missing}"
                )

            count = db.execute(
                "SELECT COUNT(*) FROM pool_snapshots"
            ).fetchone()[0]

            if count < 1:
                raise DEXExperimentError(
                    "Snapshot database contains no pool snapshots"
                )

            # Parse and validate each stored snapshot payload. This also
            # verifies the JSON shape and PoolSnapshot domain invariants.
            from app.dex.pool_data import PoolSnapshot

            for row in db.execute(
                "SELECT payload_json FROM pool_snapshots ORDER BY snapshot_id"
            ):
                try:
                    payload = json.loads(row["payload_json"])
                    PoolSnapshot.from_dict(payload)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise DEXExperimentError(
                        f"Invalid stored pool snapshot: {exc}"
                    ) from exc

            return {
                "snapshot_count": count,
                "schema_columns": sorted(columns),
            }
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise DEXExperimentError(
            f"Cannot read snapshot database: {exc}"
        ) from exc


def _open_snapshot_store(path: Path) -> tuple[sqlite3.Connection, SnapshotStore]:
    """Open a validated database read-only and wrap it without schema writes."""
    uri = path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(
        uri,
        uri=True,
        timeout=10,
        check_same_thread=False,
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")

    # SnapshotStore.__init__ creates tables and indexes. Avoid that constructor
    # here because the experiment input must remain immutable.
    store = object.__new__(SnapshotStore)
    store.db = db
    store.lock = threading.RLock()
    store.keep_per_pool = None

    return db, store


def _build_manifest(
    snapshot_path: Path,
    cutoff_timestamp: float,
    capital_micro_usdt: int,
    snapshot_sha256: str,
    snapshot_info: dict[str, Any],
) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "study_type": "dex_lp",
        "snapshot_database": {
            "filename": snapshot_path.name,
            "sha256": snapshot_sha256,
            "snapshot_count": snapshot_info["snapshot_count"],
        },
        "cutoff_timestamp": cutoff_timestamp,
        "capital_micro_usdt": capital_micro_usdt,
        "capital_unit": "micro-USDT",
        "evaluation": {
            "model": "DEXOutOfSampleEvaluator",
            "max_anchor_gap_hours": 48.0,
            "costs": "default ExecutionCostConfig",
        },
    }


def _verify_cached_artifacts(
    run: dict[str, Any],
    expected_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify a completed cached run before returning it."""
    if run.get("study_type") != "dex_lp":
        raise DEXExperimentError(
            "Cached experiment has an unexpected study type"
        )

    result_path_value = run.get("result_path")
    result_hash = run.get("result_sha256")

    if not result_path_value or not result_hash:
        raise DEXExperimentError(
            "Cached experiment is missing its result artifact reference"
        )

    result_path = Path(result_path_value).resolve()
    if not result_path.is_file():
        raise DEXExperimentError(
            "Cached result artifact is missing"
        )

    raw = result_path.read_bytes()
    if _sha256_bytes(raw) != result_hash:
        raise DEXExperimentError(
            "Cached result artifact checksum does not match the registry"
        )

    manifest_path = result_path.parent / "manifest.lock.json"
    if not manifest_path.is_file():
        raise DEXExperimentError(
            "Cached frozen manifest is missing"
        )

    try:
        stored_manifest = json.loads(manifest_path.read_text("utf-8"))
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise DEXExperimentError(
            f"Cached experiment artifact cannot be parsed: {exc}"
        ) from exc

    if _canonical_json(stored_manifest) != _canonical_json(expected_manifest):
        raise DEXExperimentError(
            "Cached frozen manifest does not match the requested inputs"
        )

    if report.get("manifest") != expected_manifest:
        raise DEXExperimentError(
            "Cached report manifest does not match the requested inputs"
        )

    experiment = report.get("experiment", {})
    if experiment.get("id") != run.get("id"):
        raise DEXExperimentError(
            "Cached report experiment ID does not match the registry"
        )

    if experiment.get("fingerprint") != run.get("fingerprint"):
        raise DEXExperimentError(
            "Cached report fingerprint does not match the registry"
        )

    return dict(run, reused=True, report=report)


def run_dex_experiment(
    snapshot_database: str | Path,
    cutoff_timestamp: float,
    capital_micro_usdt: int,
    *,
    registry_path: str | Path = "data/experiments.sqlite3",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    repeat: bool = False,
) -> dict[str, Any]:
    """Run or reuse a reproducible DEX OOS experiment."""
    snapshot_path = Path(snapshot_database).expanduser().resolve()
    cutoff = _validate_number("cutoff_timestamp", cutoff_timestamp)
    capital = _validate_capital(capital_micro_usdt)

    snapshot_info = _validate_snapshot_database(snapshot_path)
    snapshot_hash = _sha256_file(snapshot_path)
    manifest = _build_manifest(
        snapshot_path,
        cutoff,
        capital,
        snapshot_hash,
        snapshot_info,
    )

    code_hash = _implementation_hash()
    fingerprint_payload = {
        "manifest": manifest,
        "implementation_sha256": code_hash,
        "python_version": platform.python_version(),
    }
    fingerprint = _sha256_bytes(
        _canonical_json(fingerprint_payload).encode("utf-8")
    )

    registry: Registry | None = None
    identifier: str | None = None
    snapshot_db: sqlite3.Connection | None = None

    try:
        registry = Registry(registry_path)

        if not repeat:
            row = registry.db.execute(
                """
                SELECT *
                FROM runs
                WHERE fingerprint = ?
                  AND status = 'completed'
                  AND study_type = 'dex_lp'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()

            if row is not None:
                from app.experiments import unpack

                return _verify_cached_artifacts(
                    unpack(row),
                    manifest,
                )

        identifier = registry.start(
            fingerprint,
            manifest,
            code_hash,
            study_type="dex_lp",
        )

        folder = Path(output_dir).expanduser().resolve() / identifier
        folder.mkdir(parents=True, exist_ok=False)

        write_artifact(folder / "manifest.lock.json", manifest)

        # Recheck the input immediately before evaluation. This detects a
        # database replacement between initial validation and execution.
        if _sha256_file(snapshot_path) != snapshot_hash:
            raise DEXExperimentError(
                "Snapshot database changed after it was fingerprinted"
            )

        snapshot_db, store = _open_snapshot_store(snapshot_path)

        evaluator = DEXOutOfSampleEvaluator(store)
        result = evaluator.run(
            cutoff_timestamp=cutoff,
            capital_micro_usdt=capital,
        )

        report = {
            "manifest": manifest,
            "experiment": {
                "id": identifier,
                "fingerprint": fingerprint,
                "implementation_sha256": code_hash,
                "python_version": platform.python_version(),
            },
            "result": result.summary(),
        }

        report_checksum = write_artifact(
            folder / "report.json",
            report,
        )

        summary = result.summary()
        registry.finish(
            identifier,
            "completed",
            result_path=folder / "report.json",
            result_sha256=report_checksum,
            summary=summary,
        )

        completed_id = identifier
        identifier = None

        completed = read_runs(registry_path, completed_id)
        if completed is None:
            raise DEXExperimentError(
                "Completed experiment could not be read from registry"
            )

        return dict(completed, reused=False, report=report)

    except BaseException as exc:
        if registry is not None and identifier is not None:
            try:
                registry.finish(
                    identifier,
                    (
                        "interrupted"
                        if isinstance(exc, KeyboardInterrupt)
                        else "failed"
                    ),
                    error=str(exc) or type(exc).__name__,
                )
            except (sqlite3.Error, ValueError):
                pass
        raise

    finally:
        if snapshot_db is not None:
            snapshot_db.close()
        if registry is not None:
            registry.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a reproducible DEX LP out-of-sample experiment."
    )
    parser.add_argument(
        "--database", required=True,
        help="Frozen DEX snapshot SQLite database.",
    )
    parser.add_argument(
        "--cutoff", required=True, type=float,
        help="Decision cutoff as Unix timestamp.",
    )
    parser.add_argument(
        "--capital-micro-usdt", required=True, type=int,
        help="Starting capital in micro-USDT.",
    )
    parser.add_argument("--registry", default="data/experiments.sqlite3")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--repeat", action="store_true",
        help="Run again even if a verified result exists.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        result = run_dex_experiment(
            args.database,
            args.cutoff,
            args.capital_micro_usdt,
            registry_path=args.registry,
            output_dir=args.output_dir,
            repeat=args.repeat,
        )
    except (DEXExperimentError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"DEX experiment failed: {exc}\n")

    print(
        json.dumps(
            {
                "id": result["id"],
                "status": result["status"],
                "study_type": result["study_type"],
                "reused": result["reused"],
                "result_path": result.get("result_path"),
                "summary": result.get("summary"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
