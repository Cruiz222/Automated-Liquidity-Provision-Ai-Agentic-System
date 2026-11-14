import dataclasses
import math
import unittest
from pathlib import Path

from app.dex import PoolCategory, PoolSnapshot, load_snapshots
from app.dex import PoolSelectionError, PoolSelector, SelectorConfig

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "pools_snapshot.json"
SNAPSHOTS, META = load_snapshots(EXAMPLE)
NOW = META["as_of"]
USDT = 1_000_000


def selector(config=None, pools=SNAPSHOTS):
    s = PoolSelector(config)
    s.add_pools(pools)
    return s


def by_id(snapshots, pool_id):
    return next(p for p in snapshots if p.pool_id == pool_id)


class TestEvaluation(unittest.TestCase):
    def test_risk_score_is_populated_and_enforced(self):
        # Regression: the risk score used to stay 0.0 so the risk limit never applied.
        sel = selector()
        trap = sel.evaluate(by_id(SNAPSHOTS, "newfi-usdc-base-30bp"), NOW)
        self.assertGreater(trap.risk_score, 0.8)
        self.assertGreater(trap.net_apr, 0.3)            # looks great on yield alone...
        self.assertFalse(trap.eligible)                  # ...but is rejected on risk
        self.assertTrue(any("risk score" in r for r in trap.reasons))
        stable = sel.evaluate(by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp"), NOW)
        self.assertLess(stable.risk_score, 0.3)
        self.assertTrue(stable.eligible)

    def test_fee_apr_formula(self):
        snap = dataclasses.replace(by_id(SNAPSHOTS, "weth-usdc-arbitrum-5bp"),
                                   volume_usd_1d=70e6, volume_usd_7d=490e6, tvl_usd=70e6, fee_tier_bps=5)
        ev = selector().evaluate(snap, NOW)
        self.assertAlmostEqual(ev.fee_apr, 0.0005 * 1.0 * 365, places=6)

    def test_lower_of_modelled_and_reported_fee_is_used(self):
        snap = dataclasses.replace(by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp"), reported_fee_apr=0.01)
        ev = selector().evaluate(snap, NOW)
        self.assertAlmostEqual(ev.fee_apr, 0.01)
        self.assertTrue(any("differ" in w for w in ev.warnings))

    def test_base_apy_alone_is_not_used_as_fee_apr(self):
        snap = dataclasses.replace(
            by_id(SNAPSHOTS, "weth-usdc-arbitrum-5bp"),
            fee_tier_bps=None, reported_fee_apr=None, reported_base_apy=0.20,
        )
        ev = selector().evaluate(snap, NOW)
        self.assertEqual(ev.fee_apr, 0.0)
        self.assertTrue(any("fee data" in reason for reason in ev.reasons))

    def test_uncorrelated_pairs_need_stronger_fee_coverage(self):
        snap = dataclasses.replace(by_id(SNAPSHOTS, "weth-usdc-arbitrum-5bp"),
                                   volume_usd_1d=30e6, volume_usd_7d=210e6)
        ev = selector().evaluate(snap, NOW)
        self.assertFalse(ev.eligible)
        self.assertTrue(any("cover only" in r or "net APR" in r for r in ev.reasons))

    def test_stale_and_future_snapshots_excluded(self):
        snap = by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp")
        self.assertTrue(any("stale" in r for r in selector().evaluate(snap, NOW + 48 * 3600).reasons))
        self.assertTrue(any("future" in r for r in selector().evaluate(snap, NOW - 3600).reasons))

    def test_missing_data_is_an_exclusion_not_a_guess(self):
        eth = by_id(SNAPSHOTS, "weth-usdc-arbitrum-5bp")
        no_history = dataclasses.replace(eth, price_history=())
        self.assertTrue(any("price history" in r for r in selector().evaluate(no_history, NOW).reasons))
        no_volume = dataclasses.replace(eth, volume_usd_1d=None, volume_usd_7d=None)
        self.assertTrue(any("volume" in r for r in selector().evaluate(no_volume, NOW).reasons))
        no_fee = dataclasses.replace(eth, fee_tier_bps=None, reported_fee_apr=None)
        self.assertTrue(any("fee data" in r for r in selector().evaluate(no_fee, NOW).reasons))

    def test_stablecoin_without_history_gets_assumed_volatility_and_warning(self):
        stable = dataclasses.replace(by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp"), price_history=())
        ev = selector().evaluate(stable, NOW)
        self.assertTrue(ev.eligible)
        self.assertTrue(any("assuming" in w for w in ev.warnings))

    def test_depeg_history_adds_haircut_and_risk(self):
        stable = by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp")
        drifted = dataclasses.replace(stable, price_history=stable.price_history[:-1] + (0.97,))
        base, bad = selector().evaluate(stable, NOW), selector().evaluate(drifted, NOW)
        self.assertGreater(bad.depeg_haircut_apr, base.depeg_haircut_apr)
        self.assertGreater(bad.risk_score, base.risk_score)

    def test_timestamped_hourly_history_drives_volatility_and_depeg(self):
        stable = by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp")
        start = NOW - 29 * 86400
        observations = tuple(
            (start + i * 3600, 1.02)
            for i in range(29 * 24 + 1)
        )
        snap = dataclasses.replace(
            stable,
            price_history=(1.0,) * 30,
            price_observations=observations,
        )
        ev = selector().evaluate(snap, NOW)
        self.assertAlmostEqual(ev.volatility, 0.02)
        self.assertGreater(ev.depeg_haircut_apr, 0.0)

    def test_timestamped_history_requires_calendar_coverage(self):
        volatile = by_id(SNAPSHOTS, "weth-usdc-arbitrum-5bp")
        start = NOW - 2 * 86400
        observations = tuple(
            (start + i * 3600, 3000.0 * math.exp(0.001 * i))
            for i in range(49)
        )
        snap = dataclasses.replace(
            volatile,
            price_history=(),
            price_observations=observations,
        )
        ev = selector().evaluate(snap, NOW)
        self.assertIsNone(ev.volatility)
        self.assertTrue(any("coverage" in reason for reason in ev.reasons))

    def test_timestamped_history_rejects_excessive_observation_gaps(self):
        volatile = by_id(SNAPSHOTS, "weth-usdc-arbitrum-5bp")
        start = NOW - 20 * 86400
        observations = (
            (start, 3000.0),
            (start + 3600, 3010.0),
            (NOW, 3020.0),
        )
        snap = dataclasses.replace(
            volatile,
            price_history=(),
            price_observations=observations,
        )
        ev = selector().evaluate(snap, NOW)
        self.assertIsNone(ev.volatility)
        self.assertTrue(any("exceeds maximum" in reason for reason in ev.reasons))

    def test_categories_follow_token_attributes(self):
        sel = selector()
        cats = {p.pool_id: sel.evaluate(p, NOW).category for p in SNAPSHOTS}
        self.assertEqual(cats["usdc-usdt-arbitrum-1bp"], PoolCategory.STABLECOIN_STABLECOIN)
        self.assertEqual(cats["weth-cbeth-base-5bp"], PoolCategory.CORRELATED_VOLATILE)
        self.assertEqual(cats["weth-usdc-arbitrum-5bp"], PoolCategory.UNCORRELATED_VOLATILE)


class TestSelection(unittest.TestCase):
    def test_no_hardcoded_pools(self):
        with self.assertRaises(PoolSelectionError):
            PoolSelector().select(100 * USDT)
        one = selector(pools=[by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp")]).select(1000 * USDT, now=NOW)
        self.assertEqual(list(one.allocation.allocations), ["usdc-usdt-arbitrum-1bp"])

    def test_example_selection_at_modest_capital(self):
        result = selector().select(3000 * USDT, now=NOW)
        chosen = set(result.allocation.allocations)
        self.assertEqual(chosen, {"usdc-usdt-arbitrum-1bp", "dai-usdc-polygon-5bp",
                                  "weth-cbeth-base-5bp", "weth-usdc-arbitrum-5bp"})
        self.assertIn("TVL", result.excluded["pepe-weth-ethereum-30bp"][0])
        self.assertIn("risk score", result.excluded["newfi-usdc-base-30bp"][0])
        self.assertIn("gas", result.excluded["weth-usdc-ethereum-5bp"][0])

    def test_larger_capital_makes_mainnet_gas_affordable_when_yield_is_enough(self):
        cfg = SelectorConfig(gas_usd={"ethereum": 8.0, "arbitrum": 0.4, "base": 0.2, "polygon": 0.15})
        result = selector(cfg).select(100_000 * USDT, now=NOW)
        self.assertIn("wbtc-usdc-arbitrum-5bp", result.allocation.allocations)

    def test_allocation_respects_reserve_and_caps(self):
        capital = 100_000 * USDT
        result = selector().select(capital, now=NOW)
        alloc = result.allocation
        self.assertEqual(alloc.reserve_minor, capital - int(capital * 0.9))
        self.assertLessEqual(alloc.allocated_minor, int(capital * 0.9))
        self.assertEqual(alloc.allocated_minor + alloc.unallocated_minor + alloc.reserve_minor, capital)
        deployable = int(capital * 0.9)
        cfg = SelectorConfig()
        for pool_id, amount in alloc.allocations.items():
            ev = next(e for e in result.evaluations if e.pool_id == pool_id)
            self.assertLessEqual(amount, cfg.max_pool_weight * deployable + 1)
            self.assertLessEqual(amount, cfg.max_pool_tvl_share * ev.tvl_usd * USDT + 1)
        per_category = {}
        for pool_id, amount in alloc.allocations.items():
            ev = next(e for e in result.evaluations if e.pool_id == pool_id)
            per_category[ev.category] = per_category.get(ev.category, 0) + amount
        for category, total in per_category.items():
            self.assertLessEqual(total, cfg.category_limits[category].max_weight * deployable + 1)

    def test_tvl_share_cap_limits_position_in_small_pool(self):
        small = dataclasses.replace(by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp"), tvl_usd=2e6,
                                    volume_usd_1d=3e6, volume_usd_7d=21e6)
        result = selector(pools=[small]).select(1_000_000 * USDT, now=NOW)
        self.assertLessEqual(result.allocation.allocations["usdc-usdt-arbitrum-1bp"], 0.01 * 2e6 * USDT)
        self.assertGreater(result.allocation.unallocated_minor, 0)

    def test_no_eligible_pools_leaves_everything_unallocated(self):
        result = selector(pools=[by_id(SNAPSHOTS, "pepe-weth-ethereum-30bp")]).select(1000 * USDT, now=NOW)
        self.assertEqual(result.allocation.allocations, {})
        self.assertEqual(result.allocation.allocated_minor, 0)
        self.assertIn("excluded", result.allocation.rationale)

    def test_selection_is_deterministic(self):
        a = selector().select(5000 * USDT, now=NOW).allocation.allocations
        b = selector().select(5000 * USDT, now=NOW).allocation.allocations
        self.assertEqual(a, b)

    def test_invalid_capital(self):
        for bad in (0, -5, 1.5, True):
            with self.assertRaises(PoolSelectionError):
                selector().select(bad, now=NOW)

    def test_add_pool_replaces_by_id_and_rejects_non_snapshots(self):
        sel = selector()
        with self.assertRaises(ValueError):
            sel.add_pool({"pool_id": "x"})
        before = len(sel.pools)
        sel.add_pool(by_id(SNAPSHOTS, "usdc-usdt-arbitrum-1bp"))
        self.assertEqual(len(sel.pools), before)

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            SelectorConfig(reserve_fraction=1)
        with self.assertRaises(ValueError):
            SelectorConfig(horizon_days=0)

    def test_evaluation_serialises(self):
        ev = selector().evaluate(SNAPSHOTS[0], NOW).to_dict()
        self.assertEqual(ev["category"], "stablecoin-stablecoin")


if __name__ == "__main__":
    unittest.main()
