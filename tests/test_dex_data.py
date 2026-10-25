import dataclasses
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

    def test_history_is_pruned(self):
        for i in range(6):
            self.store.save(dataclasses.replace(self.base, as_of=self.base.as_of + i))
        self.assertEqual(len(self.store.history(self.base.pool_id, 50)), 3)
        with self.assertRaises(ValueError):
            self.store.history(self.base.pool_id, 0)


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


if __name__ == "__main__":
    unittest.main()
