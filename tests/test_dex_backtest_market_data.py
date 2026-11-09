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
        token_addresses=(),
        chain="ethereum",
        source="test",
    ):
        return PoolSnapshot(
            pool_id="pool-1",
            chain=chain,
            dex="uniswap",
            token0=token0,
            token1=token1,
            token_addresses=token_addresses,
            tvl_usd=100_000,
            price_observations=tuple(observations),
            as_of=as_of,
            source=source,
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

    def test_rejects_mixed_sources_for_one_pool(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
            source="provider-a",
        ))
        self.save(self.make_snapshot(
            200,
            [(20, 3100)],
            source="provider-b",
        ))

        with self.assertRaisesRegex(ValueError, "inconsistent data source"):
            self.loader.load_pool("pool-1")

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

    def test_snapshot_collection_time_bounds_are_inclusive(self):
        self.save(self.make_snapshot(100, [(10, 3000)]))
        self.save(self.make_snapshot(200, [(20, 3100)]))
        self.save(self.make_snapshot(300, [(30, 3200)]))

        result = self.loader.load_pool(
            "pool-1",
            start_as_of=200,
            end_as_of=300,
        )

        self.assertEqual(
            [(point.timestamp, point.price) for point in result.observations],
            [(20, 3100), (30, 3200)],
        )

    def test_invalid_snapshot_collection_time_bounds_fail(self):
        with self.assertRaises(ValueError):
            self.loader.load_pool("pool-1", start_as_of=300, end_as_of=200)

        with self.assertRaises(ValueError):
            self.loader.load_pool("pool-1", end_as_of=float("nan"))

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

    def test_preserves_token_contract_addresses(self):
        addresses = ("0xAbC123", "0xDeF456")
        self.save(self.make_snapshot(
            100,
            [(10, 3000), (20, 3100)],
            token_addresses=addresses,
        ))

        result = self.loader.load_pool("pool-1")

        self.assertEqual(
            result.token_addresses,
            ("0xabc123", "0xdef456"),
        )

    def test_preserves_solana_base58_address_case(self):
        addresses = (
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        )
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
            token_addresses=addresses,
            chain="solana",
        ))

        result = self.loader.load_pool("pool-1")

        self.assertEqual(result.chain, "solana")
        self.assertEqual(result.token_addresses, addresses)

    def test_rejects_chain_change_across_pool_snapshots(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
            chain="ethereum",
        ))
        self.save(self.make_snapshot(
            200,
            [(20, 3100)],
            chain="solana",
        ))

        with self.assertRaisesRegex(ValueError, "inconsistent chain identity"):
            self.loader.load_pool("pool-1")

    def test_inconsistent_token_contract_addresses_fail(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
            token_addresses=("0xaaa", "0xbbb"),
        ))
        self.save(self.make_snapshot(
            200,
            [(20, 3100)],
            token_addresses=("0xccc", "0xddd"),
        ))

        with self.assertRaisesRegex(ValueError, "inconsistent token contract"):
            self.loader.load_pool("pool-1")

    def test_mixed_address_availability_fails(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
            token_addresses=("0xaaa", "0xbbb"),
        ))
        self.save(self.make_snapshot(
            200,
            [(20, 3100)],
        ))

        with self.assertRaisesRegex(ValueError, "incomplete token contract"):
            self.loader.load_pool("pool-1")

    def test_reversed_token_contract_addresses_fail(self):
        self.save(self.make_snapshot(
            100,
            [(10, 3000)],
            token_addresses=("0xaaa", "0xbbb"),
        ))
        self.save(self.make_snapshot(
            200,
            [(20, 3100)],
            token_addresses=("0xbbb", "0xaaa"),
        ))

        with self.assertRaisesRegex(ValueError, "inconsistent token contract"):
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