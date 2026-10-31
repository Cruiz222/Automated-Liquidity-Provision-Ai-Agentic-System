import sqlite3
import threading
import unittest

from app.dex.backtest.oos import (
    DEXOutOfSampleEvaluator,
    OOSBacktestError,
)
from app.dex.pool_data import PoolSnapshot
from app.dex.snapshot_store import SnapshotStore


DAY = 86_400
CUTOFF = 20 * DAY


class TestDEXOutOfSampleEvaluator(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.lock = threading.RLock()
        self.store = SnapshotStore(
            self.db,
            self.lock,
            keep_per_pool=None,
        )

    def tearDown(self):
        self.db.close()

    def make_snapshot(
        self,
        *,
        pool_id="USDC-USDT",
        as_of=CUTOFF,
        token0="USDC",
        token1="USDT",
        price1_usd=1.0,
        price_observations=None,
        price_history=(),
        reported_fee_apr=0.10,
        tvl_usd=5_000_000,
        volume_usd_1d=2_000_000,
        fee_tier_bps=30,
    ):
        if price_observations is None:
            price_observations = tuple(
                (day * DAY, 1.0 + (day % 3) * 0.0001)
                for day in range(1, 20)
            )

        return PoolSnapshot(
            pool_id=pool_id,
            chain="base",
            dex="uniswap-v2",
            token0=token0,
            token1=token1,
            tvl_usd=tvl_usd,
            fee_tier_bps=fee_tier_bps,
            volume_usd_1d=volume_usd_1d,
            reported_fee_apr=reported_fee_apr,
            price_history=price_history,
            price_observations=tuple(price_observations),
            price0_usd=1.0,
            price1_usd=price1_usd,
            as_of=as_of,
            source="test",
        )

    def add_valid_pool(self):
        observations = [
            (day * DAY, 1.0 + (day % 3) * 0.0001)
            for day in range(1, 20)
        ]
        observations.extend([
            (21 * DAY, 1.0005),
            (22 * DAY, 1.0008),
            (23 * DAY, 1.0004),
        ])

        snapshot = self.make_snapshot(
            price_observations=tuple(
                point for point in observations
                if point[0] <= CUTOFF
            )
        )
        self.store.save(snapshot)

        # A later snapshot contributes unseen market observations.
        # Its metrics must not replace the decision-time snapshot.
        future_snapshot = self.make_snapshot(
            as_of=CUTOFF + DAY,
            price_observations=tuple(observations),
            price_history=(999.0,),
            reported_fee_apr=0.50,
            tvl_usd=5_000_000,
        )
        self.store.save(future_snapshot)

    def test_uses_only_cutoff_snapshot_for_selector(self):
        self.add_valid_pool()

        selected_snapshot = self.store.latest_all_as_of(CUTOFF)[0]
        self.assertEqual(selected_snapshot.as_of, CUTOFF)
        self.assertEqual(selected_snapshot.reported_fee_apr, 0.10)

    def test_rebuilds_selector_history_without_un_timestamped_values(self):
        self.add_valid_pool()

        evaluator = DEXOutOfSampleEvaluator(self.store)
        result = evaluator.run(CUTOFF, 10_000 * 1_000_000)

        evaluation = next(
            item for item in result.selection.evaluations
            if item.pool_id == "USDC-USDT"
        )

        expected_last_training_price = (
            1.0 + (19 % 3) * 0.0001
        )
        self.assertAlmostEqual(
            evaluation.latest_price,
            expected_last_training_price,
        )
        self.assertNotEqual(evaluation.latest_price, 999.0)

    def test_test_observations_are_strictly_after_cutoff(self):
        self.add_valid_pool()

        result = DEXOutOfSampleEvaluator(self.store).run(
            CUTOFF,
            10_000 * 1_000_000,
        )

        self.assertIn(
            "USDC-USDT",
            result.pool_results,
            "The fixture pool should be eligible and selected",
        )

        pool = result.pool_results["USDC-USDT"]

        self.assertGreater(pool.first_test_timestamp, CUTOFF)
        self.assertLessEqual(pool.anchor_timestamp, CUTOFF)
        self.assertEqual(
            pool.gross_portfolio.initial_timestamp,
            pool.anchor_timestamp,
        )

    def test_excludes_non_stable_quote_token(self):
        snapshot = self.make_snapshot(
            pool_id="ETH-WETH",
            token0="ETH",
            token1="WETH",
        )
        self.store.save(snapshot)

        # No supported quote pool remains.
        with self.assertRaises(OOSBacktestError):
            DEXOutOfSampleEvaluator(self.store).run(
                CUTOFF,
                1_000 * 1_000_000,
            )

    def test_excludes_missing_quote_usd_price(self):
        snapshot = self.make_snapshot(price1_usd=None)
        self.store.save(snapshot)

        with self.assertRaises(OOSBacktestError):
            DEXOutOfSampleEvaluator(self.store).run(
                CUTOFF,
                1_000 * 1_000_000,
            )

    def test_rejects_invalid_capital(self):
        evaluator = DEXOutOfSampleEvaluator(self.store)

        with self.assertRaises(OOSBacktestError):
            evaluator.run(CUTOFF, 0)

    def test_rejects_invalid_cutoff(self):
        evaluator = DEXOutOfSampleEvaluator(self.store)

        with self.assertRaises(OOSBacktestError):
            evaluator.run(float("nan"), 1_000_000)


if __name__ == "__main__":
    unittest.main()
