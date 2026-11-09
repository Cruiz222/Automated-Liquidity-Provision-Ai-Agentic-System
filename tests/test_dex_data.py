import dataclasses
import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path

from app.dex import (
    AMMModelError, FixturePoolProvider, PoolSnapshot, SnapshotStore, evaluate_constant_product_position,
    impermanent_loss_fraction, load_snapshots, v2_il,
)
from app.ledger import WalletLedger

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "pools_snapshot.json"


class TestSnapshotRoundTrip(unittest.TestCase):
    def test_dict_roundtrip_and_strictness(self):
        for snapshot in load_snapshots(EXAMPLE)[0]:
            self.assertEqual(PoolSnapshot.from_dict(snapshot.to_dict()), snapshot)
        data = load_snapshots(EXAMPLE)[0][0].to_dict()
        with self.assertRaises(ValueError):
            PoolSnapshot.from_dict(dict(data, surprise=1))
        with self.assertRaises(ValueError):
            PoolSnapshot.from_dict(dict(data, tvl_usd=-1))


class TestSnapshotStore(unittest.TestCase):
    def setUp(self):
        self.ledger = WalletLedger()
        self.addCleanup(self.ledger.close)
        self.store = SnapshotStore(self.ledger.db, self.ledger.lock, keep_per_pool=3)
        self.base = load_snapshots(EXAMPLE)[0][0]

    def test_save_is_idempotent_and_latest_wins(self):
        newer = dataclasses.replace(self.base, as_of=self.base.as_of + 60, tvl_usd=1.0e8)
        self.assertTrue(self.store.save(self.base))
        self.assertFalse(self.store.save(self.base))
        self.assertTrue(self.store.save(newer))
        self.assertEqual(self.store.latest(self.base.pool_id).tvl_usd, 1.0e8)
        self.assertEqual(len(self.store.latest_all()), 1)
        self.assertIsNone(self.store.latest("nope"))

    def test_failed_retention_cleanup_rolls_back_new_snapshot(self):
        store = SnapshotStore(
            self.ledger.db,
            self.ledger.lock,
            keep_per_pool=1,
        )

        original = dataclasses.replace(
            self.base,
            as_of=self.base.as_of + 10,
        )
        newer = dataclasses.replace(
            self.base,
            as_of=self.base.as_of + 20,
        )

        store.save(original)

        # Force retention cleanup to fail when it tries to delete
        # the older snapshot after inserting the newer one.
        with self.ledger.lock:
            self.ledger.db.execute(
                """
                CREATE TRIGGER fail_snapshot_retention
                BEFORE DELETE ON pool_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'simulated retention failure');
                END;
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            store.save(newer)

        history = store.history_snapshots(self.base.pool_id)

        # The failed insertion must not remain in the database.
        self.assertEqual(
            [snapshot.as_of for snapshot in history],
            [original.as_of],
        )

        # A failed standalone save must not leak an open transaction.
        self.assertFalse(self.ledger.db.in_transaction)

        with self.ledger.lock:
            self.ledger.db.execute(
                "DROP TRIGGER fail_snapshot_retention"
            )

    def test_save_preserves_caller_owned_transaction(self):
        snapshot = dataclasses.replace(
            self.base,
            as_of=self.base.as_of + 100,
        )

        with self.assertRaisesRegex(RuntimeError, "force outer rollback"):
            with self.ledger.transaction():
                self.assertTrue(self.store.save(snapshot))
                self.assertTrue(self.ledger.db.in_transaction)
                raise RuntimeError("force outer rollback")

        self.assertFalse(self.ledger.db.in_transaction)
        self.assertIsNone(self.store.latest(self.base.pool_id))

    def test_history_is_pruned(self):
        for i in range(6):
            self.store.save(dataclasses.replace(self.base, as_of=self.base.as_of + i))
        self.assertEqual(len(self.store.history(self.base.pool_id, 50)), 3)
        with self.assertRaises(ValueError):
            self.store.history(self.base.pool_id, 0)


    def test_history_snapshots_returns_full_records_oldest_first(self):
        first = dataclasses.replace(
            self.base,
            as_of=self.base.as_of + 10,
            tvl_usd=10_000_000,
        )
        second = dataclasses.replace(
            self.base,
            as_of=self.base.as_of + 20,
            tvl_usd=20_000_000,
        )

        self.store.save(second)
        self.store.save(first)

        history = self.store.history_snapshots(self.base.pool_id)

        self.assertEqual(
            [snapshot.as_of for snapshot in history],
            [first.as_of, second.as_of],
        )
        self.assertEqual(
            [snapshot.tvl_usd for snapshot in history],
            [first.tvl_usd, second.tvl_usd],
        )

    def test_history_snapshots_supports_inclusive_time_bounds(self):
        first = dataclasses.replace(self.base, as_of=self.base.as_of + 10)
        second = dataclasses.replace(self.base, as_of=self.base.as_of + 20)
        third = dataclasses.replace(self.base, as_of=self.base.as_of + 30)

        for snapshot in (first, second, third):
            self.store.save(snapshot)

        history = self.store.history_snapshots(
            self.base.pool_id,
            start_as_of=second.as_of,
            end_as_of=third.as_of,
        )

        self.assertEqual(
            [snapshot.as_of for snapshot in history],
            [second.as_of, third.as_of],
        )

    def test_history_snapshots_supports_limit(self):
        snapshots = [
            dataclasses.replace(self.base, as_of=self.base.as_of + offset)
            for offset in (10, 20, 30)
        ]

        for snapshot in snapshots:
            self.store.save(snapshot)

        history = self.store.history_snapshots(
            self.base.pool_id,
            limit=2,
        )

        self.assertEqual(
            [snapshot.as_of for snapshot in history],
            [snapshots[0].as_of, snapshots[1].as_of],
        )

    def test_history_snapshots_rejects_invalid_ranges_and_limits(self):
        with self.assertRaises(ValueError):
            self.store.history_snapshots(
                self.base.pool_id,
                start_as_of=20,
                end_as_of=10,
            )

        with self.assertRaises(ValueError):
            self.store.history_snapshots(self.base.pool_id, limit=0)

        with self.assertRaises(ValueError):
            self.store.history_snapshots(
                self.base.pool_id,
                start_as_of=float("nan"),
            )

    def test_conflicting_duplicate_is_rejected(self):
        self.store.save(self.base)

        conflicting = dataclasses.replace(
            self.base,
            tvl_usd=self.base.tvl_usd + 1,
        )

        with self.assertRaises(ValueError):
            self.store.save(conflicting)

    def test_none_retention_preserves_all_snapshots(self):
        store = SnapshotStore(
            self.ledger.db,
            self.ledger.lock,
            keep_per_pool=None,
        )

        for offset in range(6):
            store.save(
                dataclasses.replace(
                    self.base,
                    as_of=self.base.as_of + offset,
                )
            )

        self.assertEqual(
            len(store.history_snapshots(self.base.pool_id)),
            6,
        )

    def test_latest_all_as_of_returns_latest_eligible_snapshot_per_pool(self):
        first = dataclasses.replace(
            self.base,
            as_of=100,
            tvl_usd=10_000_000,
        )
        second = dataclasses.replace(
            self.base,
            as_of=200,
            tvl_usd=20_000_000,
        )
        future = dataclasses.replace(
            self.base,
            as_of=300,
            tvl_usd=30_000_000,
        )

        for snapshot in (first, second, future):
            self.store.save(snapshot)

        result = self.store.latest_all_as_of(250)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].as_of, 200)
        self.assertEqual(result[0].tvl_usd, 20_000_000)

    def test_latest_all_as_of_excludes_pools_without_eligible_snapshots(self):
        first_pool = dataclasses.replace(
            self.base,
            as_of=100,
        )
        second_pool = dataclasses.replace(
            self.base,
            pool_id="another-pool",
            as_of=300,
        )

        self.store.save(first_pool)
        self.store.save(second_pool)

        result = self.store.latest_all_as_of(200)

        self.assertEqual(
            [snapshot.pool_id for snapshot in result],
            [first_pool.pool_id],
        )

    def test_latest_all_as_of_uses_latest_inserted_snapshot_on_timestamp_tie(self):
        first_source = dataclasses.replace(
            self.base,
            as_of=100,
            source="source-a",
            tvl_usd=10_000_000,
        )
        second_source = dataclasses.replace(
            self.base,
            as_of=100,
            source="source-b",
            tvl_usd=20_000_000,
        )

        self.store.save(first_source)
        self.store.save(second_source)

        result = self.store.latest_all_as_of(100)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source, "source-b")
        self.assertEqual(result[0].tvl_usd, 20_000_000)

    def test_latest_all_as_of_returns_empty_when_no_snapshots_qualify(self):
        self.store.save(
            dataclasses.replace(self.base, as_of=200)
        )

        self.assertEqual(
            self.store.latest_all_as_of(100),
            [],
        )

    def test_latest_all_as_of_rejects_invalid_cutoffs(self):
        for cutoff in (-1, float("nan"), float("inf"), True, "100"):
            with self.subTest(cutoff=cutoff):
                with self.assertRaises(ValueError):
                    self.store.latest_all_as_of(cutoff)


class TestFixtureProvider(unittest.TestCase):
    def test_restamping_keeps_synthetic_label_and_never_looks_stale(self):
        provider = FixturePoolProvider(EXAMPLE, clock=lambda: 5_000_000.0)
        pools = provider.fetch_pools()
        self.assertTrue(all(p.as_of == 5_000_000.0 for p in pools))
        self.assertTrue(all("synthetic" in p.source and "refreshed" in p.source for p in pools))
        frozen = FixturePoolProvider(EXAMPLE, restamp=False).fetch_pools()
        self.assertNotEqual(frozen[0].as_of, 5_000_000.0)


class TestAmmCalculator(unittest.TestCase):
    def test_matches_the_closed_form_il(self):
        for ratio in (0.25, 0.5, 0.9, 1.0, 1.5, 2.0, 4.0):
            self.assertAlmostEqual(float(impermanent_loss_fraction(str(ratio))), v2_il(ratio), places=9)

    def test_position_result_at_double_price(self):
        result = evaluate_constant_product_position("1", "100", "200")
        self.assertAlmostEqual(float(result.impermanent_loss_pct), -5.7191, places=3)
        self.assertLess(result.lp_value_before_fees_quote, result.hold_value_quote)

    def test_fees_can_offset_the_loss(self):
        result = evaluate_constant_product_position("1", "100", "200", accrued_fees_quote="30")
        self.assertGreater(result.net_pnl_vs_hold_quote, Decimal(0))

    def test_bad_inputs_are_rejected(self):
        for args in (("0", "100", "100"), ("1", "-5", "100"), ("1", "100", "nan"), ("x", "1", "1")):
            with self.assertRaises(AMMModelError):
                evaluate_constant_product_position(*args)




class TestPoolSnapshotCollector(unittest.TestCase):
    def setUp(self):
        from app.dex import PoolSnapshotCollector

        self.ledger = WalletLedger()
        self.addCleanup(self.ledger.close)

        self.base = load_snapshots(EXAMPLE)[0][0]
        self.store = SnapshotStore(
            self.ledger.db,
            self.ledger.lock,
            keep_per_pool=None,
        )

        class FakeProvider:
            def __init__(self, snapshots):
                self.snapshots = snapshots

            def fetch_pools(self):
                return self.snapshots

        self.FakeProvider = FakeProvider
        self.PoolSnapshotCollector = PoolSnapshotCollector

    def test_collect_once_inserts_snapshots(self):
        provider = self.FakeProvider([self.base])
        collector = self.PoolSnapshotCollector(
            provider,
            self.store,
            clock=lambda: 1000.0,
        )

        result = collector.collect_once()

        self.assertEqual(result.fetched, 1)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.duplicates, 0)
        self.assertEqual(result.failed, 0)
        self.assertTrue(result.ok)

    def test_repeated_observation_is_counted_as_duplicate(self):
        provider = self.FakeProvider([self.base])
        collector = self.PoolSnapshotCollector(
            provider,
            self.store,
            clock=lambda: 1000.0,
        )

        first = collector.collect_once()
        second = collector.collect_once()

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.duplicates, 1)

    def test_conflicting_observation_is_reported(self):
        conflicting = dataclasses.replace(
            self.base,
            tvl_usd=self.base.tvl_usd + 100,
        )

        provider = self.FakeProvider([self.base])
        collector = self.PoolSnapshotCollector(
            provider,
            self.store,
            clock=lambda: 1000.0,
        )

        collector.collect_once()
        provider.snapshots = [conflicting]

        result = collector.collect_once()

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.inserted, 0)
        self.assertFalse(result.ok)
        self.assertIn("Conflicting snapshot", result.errors[0])

    def test_invalid_observation_does_not_prevent_valid_observation(self):
        provider = self.FakeProvider([None, self.base])
        collector = self.PoolSnapshotCollector(
            provider,
            self.store,
            clock=lambda: 1000.0,
        )

        result = collector.collect_once()

        self.assertEqual(result.fetched, 2)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.failed, 1)

    def test_provider_failure_is_reported(self):
        class BrokenProvider:
            def fetch_pools(self):
                raise RuntimeError("simulated provider outage")

        collector = self.PoolSnapshotCollector(
            BrokenProvider(),
            self.store,
            clock=lambda: 1000.0,
        )

        result = collector.collect_once()

        self.assertEqual(result.fetched, 0)
        self.assertEqual(result.failed, 1)
        self.assertIn("simulated provider outage", result.errors[0])

    def test_non_list_provider_response_is_rejected(self):
        provider = self.FakeProvider([])
        provider.fetch_pools = lambda: None

        collector = self.PoolSnapshotCollector(
            provider,
            self.store,
            clock=lambda: 1000.0,
        )

        result = collector.collect_once()

        self.assertEqual(result.failed, 1)
        self.assertIn("non-list", result.errors[0])

    def test_collect_forever_uses_injected_sleep_and_stops_at_limit(self):
        sleep_calls = []
        results = []

        collector = self.PoolSnapshotCollector(
            self.FakeProvider([self.base]),
            self.store,
            clock=lambda: 1000.0,
            sleep=sleep_calls.append,
        )

        collector.collect_forever(
            interval_seconds=30,
            max_cycles=3,
            on_result=results.append,
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(sleep_calls, [30, 30])

    def test_invalid_completion_timestamp_is_rejected(self):
        timestamps = iter([1000.0, float("nan")])

        collector = self.PoolSnapshotCollector(
            self.FakeProvider([self.base]),
            self.store,
            clock=lambda: next(timestamps),
        )

        with self.assertRaises(ValueError):
            collector.collect_once()

    def test_result_can_be_serialized(self):
        collector = self.PoolSnapshotCollector(
            self.FakeProvider([self.base]),
            self.store,
            clock=lambda: 1000.0,
        )

        result = collector.collect_once()
        report = result.to_dict()

        self.assertTrue(report["ok"])
        self.assertEqual(report["inserted"], 1)
        self.assertIn("duration_seconds", report)


if __name__ == "__main__":
    unittest.main()
