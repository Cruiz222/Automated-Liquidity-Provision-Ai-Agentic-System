import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app.dex.backtest import (
    EligibilityConfig,
    EligibilityStatus,
    IdentityConfidence,
    build_eligibility_report,
    build_eligibility_report_from_database,
)
from app.dex.pool_data import PoolSnapshot
from app.dex.snapshot_store import SnapshotStore


class TestHistoricalDataEligibility(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = SnapshotStore(self.db, threading.RLock())
        self.as_of = 1_800_000_000

    def tearDown(self):
        self.db.close()

    def observations(self, count=720, missing_indices=(), latest_age_hours=0):
        points = []
        latest_timestamp = self.as_of - latest_age_hours * 3600

        for index in range(count):
            if index in missing_indices:
                continue

            timestamp = latest_timestamp - (count - 1 - index) * 3600
            points.append((timestamp, 2000.0 + index))

        return tuple(points)

    def snapshot(
        self,
        pool_id="pool-1",
        *,
        token0="ETH",
        token1="USDC",
        addresses=("0xaaa", "0xbbb"),
        observations=None,
        as_of=None,
        dex="uniswap",
    ):
        return PoolSnapshot(
            pool_id=pool_id,
            chain="ethereum",
            dex=dex,
            token0=token0,
            token1=token1,
            tvl_usd=100_000,
            token_addresses=tuple(addresses),
            price_observations=(
                self.observations()
                if observations is None
                else tuple(observations)
            ),
            as_of=self.as_of if as_of is None else as_of,
            source="test",
        )

    def save(self, snapshot):
        self.store.save(snapshot)
        self.db.commit()

    def test_complete_history_is_eligible(self):
        self.save(self.snapshot())

        report = build_eligibility_report(self.store)

        self.assertEqual(report.eligible_count, 1)
        self.assertEqual(report.rejected_count, 0)
        self.assertEqual(
            report.pools[0].status,
            EligibilityStatus.ELIGIBLE,
        )
        self.assertEqual(report.pools[0].observation_count, 720)
        self.assertEqual(report.pools[0].count_coverage_pct, 100.0)
        self.assertEqual(
            report.pools[0].identity_confidence,
            IdentityConfidence.HIGH,
        )

    def test_duplicate_token_pair_is_grouped_not_rejected(self):
        self.save(self.snapshot(pool_id="pool-1"))
        self.save(
            self.snapshot(
                pool_id="pool-2",
                token0="USDC",
                token1="ETH",
                addresses=("0xbbb", "0xaaa"),
                dex="sushiswap",
            )
        )

        report = build_eligibility_report(self.store)

        self.assertEqual(report.eligible_count, 2)
        self.assertEqual(len(report.market_groups), 1)
        self.assertFalse(report.market_groups[0].is_single_pool_group)
        self.assertEqual(report.market_group_count, 1)
        self.assertEqual(report.market_groups[0].chain, "ethereum")
        self.assertEqual(report.market_groups[0].token_pair, "ETH/USDC")
        self.assertEqual(
            set(report.market_groups[0].pool_ids),
            {"pool-1", "pool-2"},
        )

    def test_symbol_fallback_requires_review(self):
        self.save(self.snapshot(addresses=()))

        report = build_eligibility_report(self.store)

        self.assertEqual(
            report.pools[0].status,
            EligibilityStatus.NEEDS_REVIEW,
        )
        self.assertEqual(
            report.pools[0].identity_confidence,
            IdentityConfidence.LOW,
        )

    def test_three_hour_gap_requires_review(self):
        # Removing two consecutive hourly points creates a three-hour gap.
        self.save(
            self.snapshot(
                observations=self.observations(
                    missing_indices={300, 301}
                )
            )
        )

        report = build_eligibility_report(self.store)

        self.assertEqual(
            report.pools[0].status,
            EligibilityStatus.NEEDS_REVIEW,
        )
        self.assertGreater(report.pools[0].largest_gap_hours, 2.0)

    def test_low_coverage_is_rejected(self):
        # Keep only 600 observations, below the 90% threshold.
        self.save(
            self.snapshot(
                observations=self.observations(count=600)
            )
        )

        report = build_eligibility_report(self.store)

        self.assertEqual(
            report.pools[0].status,
            EligibilityStatus.REJECTED,
        )
        self.assertLess(report.pools[0].count_coverage_pct, 90.0)

    def test_stale_history_requires_review(self):
        self.save(
            self.snapshot(
                observations=self.observations(latest_age_hours=2)
            )
        )

        report = build_eligibility_report(self.store)

        self.assertEqual(
            report.pools[0].status,
            EligibilityStatus.NEEDS_REVIEW,
        )
        self.assertGreater(
            report.pools[0].latest_observation_age_minutes,
            90,
        )

    def test_conflicting_overlapping_prices_are_rejected(self):
        first = self.snapshot(
            observations=((self.as_of - 3600, 2000.0),),
            as_of=self.as_of - 100,
        )
        second = self.snapshot(
            observations=((self.as_of - 3600, 2100.0),),
            as_of=self.as_of,
        )

        self.save(first)
        self.save(second)

        report = build_eligibility_report(self.store)

        self.assertEqual(
            report.pools[0].status,
            EligibilityStatus.REJECTED,
        )
        self.assertTrue(
            any("conflicting prices" in reason
                for reason in report.pools[0].reasons)
        )

    def test_report_from_database_uses_read_only_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshots.sqlite3"

            writable_db = sqlite3.connect(path)
            writable_store = SnapshotStore(
                writable_db,
                threading.RLock(),
            )
            writable_store.save(self.snapshot())
            writable_db.commit()
            writable_db.close()

            report = build_eligibility_report_from_database(path)

            self.assertEqual(report.eligible_count, 1)

            readonly_db = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro",
                uri=True,
            )
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    readonly_db.execute(
                        "CREATE TABLE should_not_exist (id INTEGER)"
                    )
            finally:
                readonly_db.close()

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            EligibilityConfig(expected_points=1)

        with self.assertRaises(ValueError):
            EligibilityConfig(
                eligible_coverage_pct=80,
                review_coverage_pct=90,
            )

        with self.assertRaises(ValueError):
            EligibilityConfig(eligible_coverage_pct=101)

        with self.assertRaises(ValueError):
            EligibilityConfig(review_coverage_pct=101)


    def test_coverage_threshold_boundaries(self):
        config = EligibilityConfig(expected_points=100)

        self.save(
            self.snapshot(
                pool_id="coverage-98",
                observations=self.observations(count=98),
            )
        )
        self.save(
            self.snapshot(
                pool_id="coverage-90",
                observations=self.observations(count=90),
            )
        )

        report = build_eligibility_report(self.store, config)

        results = {pool.pool_id: pool for pool in report.pools}

        self.assertAlmostEqual(
            results["coverage-98"].count_coverage_pct, 98.0
        )
        self.assertEqual(
            results["coverage-98"].status,
            EligibilityStatus.ELIGIBLE,
        )

        self.assertAlmostEqual(
            results["coverage-90"].count_coverage_pct, 90.0
        )
        self.assertEqual(
            results["coverage-90"].status,
            EligibilityStatus.NEEDS_REVIEW,
        )

    def test_gap_threshold_boundaries(self):
        self.save(
            self.snapshot(
                pool_id="gap-2h",
                observations=self.observations(
                    missing_indices={300}
                ),
            )
        )
        self.save(
            self.snapshot(
                pool_id="gap-6h",
                observations=self.observations(
                    missing_indices={300, 301, 302, 303, 304}
                ),
            )
        )

        report = build_eligibility_report(self.store)

        results = {pool.pool_id: pool for pool in report.pools}

        self.assertAlmostEqual(
            results["gap-2h"].largest_gap_hours, 2.0
        )
        self.assertEqual(
            results["gap-2h"].status,
            EligibilityStatus.ELIGIBLE,
        )

        self.assertAlmostEqual(
            results["gap-6h"].largest_gap_hours, 6.0
        )
        self.assertEqual(
            results["gap-6h"].status,
            EligibilityStatus.NEEDS_REVIEW,
        )

    def test_freshness_threshold_boundaries(self):
        observations_90m = tuple(
            (timestamp - 90 * 60, price)
            for timestamp, price in self.observations()
        )
        observations_360m = tuple(
            (timestamp - 360 * 60, price)
            for timestamp, price in self.observations()
        )

        self.save(
            self.snapshot(
                pool_id="age-90m",
                observations=observations_90m,
            )
        )
        self.save(
            self.snapshot(
                pool_id="age-360m",
                observations=observations_360m,
            )
        )

        report = build_eligibility_report(self.store)

        results = {pool.pool_id: pool for pool in report.pools}

        self.assertAlmostEqual(
            results["age-90m"].latest_observation_age_minutes, 90.0
        )
        self.assertEqual(
            results["age-90m"].status,
            EligibilityStatus.ELIGIBLE,
        )

        self.assertAlmostEqual(
            results["age-360m"].latest_observation_age_minutes, 360.0
        )
        self.assertEqual(
            results["age-360m"].status,
            EligibilityStatus.NEEDS_REVIEW,
        )


if __name__ == "__main__":
    unittest.main()
