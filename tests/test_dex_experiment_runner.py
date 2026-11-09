import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.dex.backtest.experiment_runner import (
    DEXExperimentError,
    _implementation_hash,
    run_dex_experiment,
)
from app.dex.pool_data import PoolSnapshot
from app.dex.snapshot_store import SnapshotStore
from app.experiments import read_runs


DAY = 86_400
CUTOFF = 20 * DAY
CAPITAL = 10_000 * 1_000_000


class TestDEXExperimentRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot_path = self.root / "snapshots.sqlite3"
        self.registry_path = self.root / "registry.sqlite3"
        self.output_dir = self.root / "artifacts"

        self.create_snapshot_database()

    def tearDown(self):
        self.temp.cleanup()

    def make_snapshot(
        self, *, as_of, observations, fee_apr=0.10, source="test"
    ):
        return PoolSnapshot(
            pool_id="USDC-USDT",
            chain="base",
            dex="uniswap-v2",
            token0="USDC",
            token1="USDT",
            tvl_usd=5_000_000,
            fee_tier_bps=30,
            volume_usd_1d=2_000_000,
            reported_fee_apr=fee_apr,
            price_observations=tuple(observations),
            price0_usd=1.0,
            price1_usd=1.0,
            as_of=as_of,
            source=source,
        )

    def create_snapshot_database(self):
        observations = [
            (day * DAY, 1.0 + (day % 3) * 0.0001)
            for day in range(1, 20)
        ]
        observations.extend([
            (21 * DAY, 1.0005),
            (22 * DAY, 1.0008),
            (23 * DAY, 1.0004),
        ])

        db = sqlite3.connect(self.snapshot_path)
        try:
            store = SnapshotStore(
                db,
                threading.RLock(),
                keep_per_pool=None,
            )
            store.save(
                self.make_snapshot(
                    as_of=CUTOFF,
                    observations=[
                        point for point in observations
                        if point[0] <= CUTOFF
                    ],
                )
            )
            store.save(
                self.make_snapshot(
                    as_of=CUTOFF + DAY,
                    observations=observations,
                    fee_apr=0.50,
                )
            )
            db.commit()
        finally:
            db.close()

    def run_experiment(self, **overrides):
        args = {
            "snapshot_database": self.snapshot_path,
            "cutoff_timestamp": CUTOFF,
            "capital_micro_usdt": CAPITAL,
            "registry_path": self.registry_path,
            "output_dir": self.output_dir,
        }
        args.update(overrides)
        return run_dex_experiment(**args)

    def test_success_creates_report_and_registry_record(self):
        result = self.run_experiment()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["study_type"], "dex_lp")
        self.assertFalse(result["reused"])

        report_path = Path(result["result_path"])
        self.assertTrue(report_path.is_file())

        report = json.loads(report_path.read_text("utf-8"))
        self.assertEqual(report["manifest"]["study_type"], "dex_lp")
        self.assertEqual(
            report["manifest"]["snapshot_database"]["source"],
            "test",
        )
        self.assertEqual(report["experiment"]["id"], result["id"])
        self.assertIn("selected_pool_count", report["result"])

        raw = report_path.read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            result["result_sha256"],
        )

        registered = read_runs(self.registry_path, result["id"])
        self.assertEqual(registered["status"], "completed")
        self.assertEqual(registered["study_type"], "dex_lp")

    def test_mixed_source_database_is_rejected(self):
        db = sqlite3.connect(self.snapshot_path)
        try:
            store = SnapshotStore(
                db,
                threading.RLock(),
                keep_per_pool=None,
            )
            store.save(
                self.make_snapshot(
                    as_of=CUTOFF + 2 * DAY,
                    observations=[(CUTOFF + 2 * DAY, 1.0006)],
                    source="another-provider",
                )
            )
            db.commit()
        finally:
            db.close()

        with self.assertRaisesRegex(
            DEXExperimentError,
            "exactly one data source",
        ):
            self.run_experiment()

    def test_sqlite_snapshot_metadata_mismatch_is_rejected(self):
        db = sqlite3.connect(self.snapshot_path)
        try:
            db.execute(
                """
                UPDATE pool_snapshots
                SET source = ?
                WHERE snapshot_id = (
                    SELECT MIN(snapshot_id) FROM pool_snapshots
                )
                """,
                ("tampered-provider",),
            )
            db.commit()
        finally:
            db.close()

        with self.assertRaisesRegex(
            DEXExperimentError,
            "metadata does not match",
        ):
            self.run_experiment()

    def test_second_call_reuses_verified_result(self):
        first = self.run_experiment()
        second = self.run_experiment()

        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["reused"])

    def test_repeat_creates_a_new_run(self):
        first = self.run_experiment()
        second = self.run_experiment(repeat=True)

        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second["reused"])

    def test_cached_report_checksum_tampering_is_rejected(self):
        first = self.run_experiment()
        report_path = Path(first["result_path"])
        report_path.write_text('{"tampered": true}', encoding="utf-8")

        with self.assertRaises(DEXExperimentError):
            self.run_experiment()

    def test_cached_manifest_tampering_is_rejected(self):
        first = self.run_experiment()
        manifest_path = (
            Path(first["result_path"]).parent / "manifest.lock.json"
        )
        manifest_path.write_text('{"tampered": true}', encoding="utf-8")

        with self.assertRaises(DEXExperimentError):
            self.run_experiment()

    def test_missing_snapshot_database_is_rejected(self):
        missing = self.root / "missing.sqlite3"

        with self.assertRaises(DEXExperimentError):
            self.run_experiment(snapshot_database=missing)

    def test_empty_snapshot_database_is_rejected(self):
        empty = self.root / "empty.sqlite3"
        sqlite3.connect(empty).close()

        with self.assertRaises(DEXExperimentError):
            self.run_experiment(snapshot_database=empty)

    def test_snapshot_database_with_wal_sidecar_is_rejected(self):
        wal_path = Path(f"{self.snapshot_path}-wal")
        wal_path.write_bytes(b"test sidecar")

        try:
            with self.assertRaisesRegex(
                DEXExperimentError,
                "SQLite -wal sidecar",
            ):
                self.run_experiment()
        finally:
            wal_path.unlink(missing_ok=True)

    def test_snapshot_database_is_not_modified(self):
        before = hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest()

        self.run_experiment()

        after = hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_implementation_hash_is_independent_of_current_directory(self):
        original = Path.cwd()
        try:
            first = _implementation_hash()
            import os
            os.chdir(self.root)
            second = _implementation_hash()
        finally:
            os.chdir(original)

        self.assertEqual(first, second)

    def test_evaluation_failure_is_recorded(self):
        with patch(
            "app.dex.backtest.experiment_runner.DEXOutOfSampleEvaluator.run",
            side_effect=RuntimeError("injected test failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.run_experiment()

        runs = read_runs(self.registry_path)
        failed = next(run for run in runs if run["status"] == "failed")

        self.assertEqual(failed["study_type"], "dex_lp")
        self.assertIn("injected test failure", failed["error"])


if __name__ == "__main__":
    unittest.main()
