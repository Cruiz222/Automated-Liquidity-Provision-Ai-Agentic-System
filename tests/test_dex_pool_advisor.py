import unittest
from app.dex_pool_advisor import PoolCandidate, PoolInputError, evaluate_pool, rank_pools, recommend_allocation
def pool(**updates):
 d={"pool_id":"pool-usdc-usdt","chain":"base","dex":"ExampleDEX","token0":"USDC","token1":"USDT","tvl_usd":"1000000","volume_24h_usd":"500000","fee_apr_pct":"12","volatility_pct":"2","depeg_risk_pct":"3","contract_risk_pct":"5","correlation_risk_pct":"10","gas_cost_usd":"1","category":"stable_stable"}; d.update(updates); return PoolCandidate.from_dict(d)
class TestDexPoolAdvisor(unittest.TestCase):
 def test_metrics_and_warnings(self):
  r=evaluate_pool(pool()); self.assertTrue(r["eligible"]); self.assertIn("risk_penalty_points",r["metrics"]); self.assertTrue(r["warnings"])
 def test_contract_risk_blocks(self):
  r=evaluate_pool(pool(contract_risk_pct="90",fee_apr_pct="500")); self.assertFalse(r["eligible"]); self.assertEqual(r["metrics"]["score"],0)
 def test_zero_volume_blocked(self): self.assertFalse(evaluate_pool(pool(volume_24h_usd="0"))["eligible"])
 def test_bad_input(self):
  with self.assertRaises(PoolInputError): pool(tvl_usd="-1")
  with self.assertRaises(PoolInputError): pool(token1="USDC")
 def test_rank_eligible_first(self):
  r=rank_pools([pool(pool_id="bad",contract_risk_pct="99"),pool()]); self.assertEqual(r[0]["pool_id"],"pool-usdc-usdt"); self.assertFalse(r[-1]["eligible"])
 def test_allocation_is_bounded(self):
  r=recommend_allocation([pool(),pool(pool_id="pool-2",dex="OtherDEX")],"1000","0.25"); self.assertEqual(r["allocated_usd"],"500.00"); self.assertEqual(len(r["allocations"]),2); self.assertEqual(r["mode"],"recommendation_only")
 def test_invalid_cap(self):
  with self.assertRaises(PoolInputError): recommend_allocation([pool()],"1000","1.1")
if __name__=="__main__": unittest.main()
