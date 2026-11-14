import json
import tempfile
import unittest
from pathlib import Path

from app.dex import (
    PoolCategory, PoolSnapshot, load_snapshots, parse_defillama_pools, with_price_history,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "pools_snapshot.json"


def pool(**overrides):
    base = dict(pool_id="p", chain="arbitrum", dex="uniswap-v3", token0="WETH", token1="USDC", tvl_usd=1e7)
    base.update(overrides)
    return PoolSnapshot(**base)


class TestCategory(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(pool(token0="USDC", token1="usdt").category, PoolCategory.STABLECOIN_STABLECOIN)
        self.assertEqual(pool(token0="WETH", token1="wstETH").category, PoolCategory.CORRELATED_VOLATILE)
        self.assertEqual(pool(token0="WETH", token1="USDC").category, PoolCategory.UNCORRELATED_VOLATILE)

    def test_measured_correlation_can_promote_a_pair(self):
        self.assertEqual(pool(token0="WBTC", token1="WETH", correlation=0.85).category,
                         PoolCategory.CORRELATED_VOLATILE)
        self.assertEqual(pool(token0="WBTC", token1="WETH", correlation=0.5).category,
                         PoolCategory.UNCORRELATED_VOLATILE)

    def test_daily_volume_is_conservative(self):
        self.assertEqual(pool(volume_usd_1d=10, volume_usd_7d=140).daily_volume_usd(), 10)
        self.assertEqual(pool(volume_usd_1d=100, volume_usd_7d=140).daily_volume_usd(), 20)
        self.assertIsNone(pool().daily_volume_usd())


class TestValidation(unittest.TestCase):
    def test_bad_values_rejected(self):
        for overrides in ({"tvl_usd": 0}, {"tvl_usd": float("nan")}, {"fee_tier_bps": 10_000},
                          {"volume_usd_1d": -1}, {"correlation": 2}, {"price_history": (1.0, -1.0)},
                          {"price_observations": ((1.0, 2.0), (1.0, 3.0))},
                          {"price_observations": ((-1.0, 2.0),)},
                          {"price_observations": ((1.0, 0.0),)},
                          {"price_observations": ((1.0,),)},
                          {"pool_id": ""}, {"token0": " "}, {"as_of": -5}):
            with self.assertRaises(ValueError, msg=str(overrides)):
                pool(**overrides)


class TestLoading(unittest.TestCase):
    def test_example_file_loads(self):
        snapshots, meta = load_snapshots(EXAMPLE)
        self.assertEqual(len(snapshots), 8)
        self.assertIn("synthetic", meta["source"])
        self.assertTrue(all(s.as_of == meta["as_of"] for s in snapshots))

    def test_rejects_unknown_fields_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            row = dict(pool_id="a", chain="c", dex="d", token0="USDC", token1="USDT", tvl_usd=5e6)
            path.write_text(json.dumps({"pools": [dict(row, surprise=1)]}))
            with self.assertRaises(ValueError):
                load_snapshots(path)
            path.write_text(json.dumps({"pools": [row, row]}))
            with self.assertRaises(ValueError):
                load_snapshots(path)
            path.write_text(json.dumps({"nope": []}))
            with self.assertRaises(ValueError):
                load_snapshots(path)


class TestDefiLlama(unittest.TestCase):
    PAYLOAD = {"data": [
        {"pool": "abc", "chain": "Arbitrum", "project": "uniswap-v3", "symbol": "WETH-USDC", "poolMeta": "0.05%",
         "tvlUsd": 5e7, "apyBase": 12.0, "volumeUsd1d": 9e7, "volumeUsd7d": 5e8},
        {"pool": "single", "chain": "Ethereum", "project": "lido", "symbol": "STETH", "tvlUsd": 2e10},
        {"pool": "tiny", "chain": "Base", "project": "x", "symbol": "A-B", "tvlUsd": 100.0},
        {"chain": "Base", "symbol": "A-B", "tvlUsd": 1e7},
        "garbage",
    ]}

    def test_parses_two_token_pools_only(self):
        snapshots = parse_defillama_pools(
            self.PAYLOAD, as_of=123.0, min_tvl_usd=1000,
            fee_apr_projects=("uniswap-v3",),
        )
        self.assertEqual([s.pool_id for s in snapshots], ["abc"])
        snap = snapshots[0]
        self.assertEqual((snap.chain, snap.dex, snap.fee_tier_bps), ("arbitrum", "uniswap-v3", 5.0))
        self.assertAlmostEqual(snap.reported_fee_apr, 0.12)
        self.assertAlmostEqual(snap.reported_base_apy, 0.12)
        self.assertEqual(snap.source, "defillama")

    def test_untrusted_base_apy_is_not_promoted_to_fee_apr(self):
        snap = parse_defillama_pools(self.PAYLOAD, as_of=123.0)[0]
        self.assertAlmostEqual(snap.reported_base_apy, 0.12)
        self.assertIsNone(snap.reported_fee_apr)

    def test_bad_payload(self):
        with self.assertRaises(ValueError):
            parse_defillama_pools({"data": "x"}, as_of=1)

    def test_attach_price_history(self):
        snap = parse_defillama_pools(self.PAYLOAD, as_of=1.0)[0]
        self.assertEqual(with_price_history(snap, [1, 2, 3]).price_history, (1, 2, 3))

    def test_timestamped_observations_round_trip(self):
        snap = pool(price_history=(2.0, 3.0), price_observations=((100.0, 2.0), (200.0, 3.0)))
        restored = PoolSnapshot.from_dict(snap.to_dict())
        self.assertEqual(restored, snap)

    def test_timestamped_observations_load_from_json_lists(self):
        snap = pool(price_observations=[[100.0, 2.0], [200.0, 3.0]])
        self.assertEqual(snap.price_observations, ((100.0, 2.0), (200.0, 3.0)))


if __name__ == "__main__":
    unittest.main()
