import sqlite3
import threading
import unittest

from app.dex.backtest import HistoricalMarketDataLoader, PricePoint
from app.dex.pool_data import PoolSnapshot
from app.dex.snapshot_store import SnapshotStore


class TestHistoricalMarketDataLoader(unittest.TestCase):

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = SnapshotStore(self.db, threading.RLock())
        self.loader = HistoricalMarketDataLoader(self.store)

    def tearDown(self):
        self.db.close()

    def make_snapshot(
        self,
        as_of,
        observations,
        token0="ETH",
        token1="USDT",
    ):
        return PoolSnapshot(
            pool_id="pool-1",
            chain="ethereum",
            dex="uniswap",
            token0=token0,
            token1=token1,
            tvl_usd=100_000,
            price_observations=tuple(observations),
            as_of=as_of,
            source="test",
        )

    def save(self, snapshot):
        self.store.save(snapshot)
        self.db.commit()

    def test_merges_overlapping_observations(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000), (20, 3100)],
        ))
        self.save(self.make_snapshot(
            200,
            [(20, 3100), (30, 3200)],
        ))

        result = self.loader.load_pool("pool-1")

        self.assertEqual(
            [(p.timestamp, p.price) for p in result.observations],
            [(10, 3000), (20, 3100), (30, 3200)],
        )

    def test_observations_are_chronologically_sorted(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000), (30, 3200)],
        ))
        self.save(self.make_snapshot(
            200,
            [(20, 3100), (40, 3300)],
        ))

        result = self.loader.load_pool("pool-1")

        timestamps = [p.timestamp for p in result.observations]

        self.assertEqual(timestamps, [10, 20, 30, 40])

    def test_duplicate_timestamp_with_conflicting_price_fails(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
        ))
        self.save(self.make_snapshot(
            200,
            [(10, 3050)],
        ))

        with self.assertRaisesRegex(ValueError, "conflicting prices"):
            self.loader.load_pool("pool-1")

    def test_time_bounds_are_inclusive(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000), (20, 3100), (30, 3200)],
        ))

        result = self.loader.load_pool(
            "pool-1",
            start_timestamp=20,
            end_timestamp=30,
        )

        self.assertEqual(
            [p.timestamp for p in result.observations],
            [20, 30],
        )

    def test_empty_time_range_fails(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
        ))

        with self.assertRaisesRegex(ValueError, "no price observations"):
            self.loader.load_pool(
                "pool-1",
                start_timestamp=20,
                end_timestamp=30,
            )

    def test_missing_pool_fails(self):
        with self.assertRaisesRegex(ValueError, "no snapshots found"):
            self.loader.load_pool("missing-pool")

    def test_inconsistent_token_orientation_fails(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
        ))

        self.save(self.make_snapshot(
            200,
            [(20, 3100)],
            token0="USDT",
            token1="ETH",
        ))

        with self.assertRaisesRegex(ValueError, "inconsistent token orientation"):
            self.loader.load_pool("pool-1")

    def test_invalid_time_bounds_fail(self):
        with self.assertRaises(ValueError):
            self.loader.load_pool("pool-1", start_timestamp=-1)

        with self.assertRaises(ValueError):
            self.loader.load_pool(
                "pool-1",
                start_timestamp=30,
                end_timestamp=20,
            )

    def test_price_point_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            PricePoint(timestamp=-1, price=3000)

        with self.assertRaises(ValueError):
            PricePoint(timestamp=10, price=0)


if __name__ == "__main__":
    unittest.main()