import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.dex_pool_data import (
    FixturePoolProvider, PoolDataError, PoolSnapshot, PoolSnapshotStore, screen_fresh_pools,
)


def snapshot(**overrides):
    data = {
        "pool_id": "base-usdc-usdt", "chain": "base", "dex": "ExampleDEX",
        "token0": "USDC", "token1": "USDT", "tvl_usd": "1000000",
        "volume_24h_usd": "500000", "fee_apr_pct": "12",
        "observed_at": "2026-10-23T20:00:00Z", "source": "fixture",
        "category": "stable_stable", "volatility_pct": "1", "depeg_risk_pct": "2",
        "contract_risk_pct": "3", "correlation_risk_pct": "10", "gas_cost_usd": "1",
    }
    data.update(overrides)
    return PoolSnapshot.from_dict(data)


class TestPoolSnapshot(unittest.TestCase):
    def test_parse_and_convert_to_advisor(self):
        item = snapshot()
        self.assertEqual(item.pool_id, "base-usdc-usdt")
        self.assertEqual(item.to_advisor_candidate().token0, "USDC")

    def test_rejects_missing_timezone(self):
        with self.assertRaises(PoolDataError):
            snapshot(observed_at="2026-10-23T20:00:00")

    def test_rejects_identical_tokens(self):
        with self.assertRaises(PoolDataError):
            snapshot(token1="USDC")

    def test_staleness_gate(self):
        item = snapshot()
        item.validate_freshness(now=datetime(2026, 10, 23, 20, 1, tzinfo=timezone.utc), max_age_seconds=120)
        with self.assertRaises(PoolDataError):
            item.validate_freshness(now=datetime(2026, 10, 23, 20, 10, tzinfo=timezone.utc), max_age_seconds=120)

    def test_store_round_trip_and_duplicate_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            store = PoolSnapshotStore(Path(temp) / "pools.sqlite3")
            item = snapshot()
            self.assertTrue(store.save(item))
            self.assertFalse(store.save(item))
            result = store.latest("base-usdc-usdt")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].source, "fixture")
            store.close()

    def test_screening_rejects_stale_and_scores_fresh(self):
        fresh = snapshot(observed_at="2026-10-23T20:00:00Z")
        stale = snapshot(pool_id="stale-pool", observed_at="2026-10-23T19:00:00Z")
        result = screen_fresh_pools(FixturePoolProvider([fresh, stale]),
            now=datetime(2026, 10, 23, 20, 1, tzinfo=timezone.utc), max_age_seconds=120)
        self.assertEqual(result["evaluated_count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(result["rejected"][0]["pool_id"], "stale-pool")


if __name__ == "__main__":
    unittest.main()
