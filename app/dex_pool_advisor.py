"""Explainable offline DEX pool screening and allocation recommendations."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

class PoolInputError(ValueError):
    """A pool candidate has missing, malformed, or implausible inputs."""

@dataclass(frozen=True)
class PoolCandidate:
    pool_id: str; chain: str; dex: str; token0: str; token1: str
    tvl_usd: Decimal; volume_24h_usd: Decimal; fee_apr_pct: Decimal; volatility_pct: Decimal
    depeg_risk_pct: Decimal = Decimal("0"); contract_risk_pct: Decimal = Decimal("0")
    correlation_risk_pct: Decimal = Decimal("50"); gas_cost_usd: Decimal = Decimal("0")
    category: str = "uncorrelated_volatile"

    @classmethod
    def from_dict(cls, data: dict) -> "PoolCandidate":
        required=("pool_id","chain","dex","token0","token1","tvl_usd","volume_24h_usd","fee_apr_pct","volatility_pct")
        missing=[k for k in required if k not in data]
        if missing: raise PoolInputError("missing required fields: "+", ".join(missing))
        fields=("tvl_usd","volume_24h_usd","fee_apr_pct","volatility_pct","depeg_risk_pct","contract_risk_pct","correlation_risk_pct","gas_cost_usd")
        defaults={"depeg_risk_pct":0,"contract_risk_pct":0,"correlation_risk_pct":50,"gas_cost_usd":0}
        values=dict(data)
        try:
            for k in fields:
                values[k]=Decimal(str(values[k] if k in values else defaults[k]))
                if not values[k].is_finite(): raise PoolInputError(f"{k} must be finite")
        except (InvalidOperation, ValueError, TypeError): raise PoolInputError("numeric pool fields must be valid decimals") from None
        values.setdefault("category","uncorrelated_volatile")
        candidate=cls(**{k:values[k] for k in cls.__dataclass_fields__ if k in values})
        candidate.validate(); return candidate

    def validate(self) -> None:
        numeric=("tvl_usd","volume_24h_usd","fee_apr_pct","volatility_pct","depeg_risk_pct","contract_risk_pct","correlation_risk_pct","gas_cost_usd")
        for k in numeric:
            v=getattr(self,k)
            if not isinstance(v,Decimal) or not v.is_finite() or v<0: raise PoolInputError(f"{k} must be finite and non-negative")
        if self.tvl_usd<=0: raise PoolInputError("tvl_usd must be greater than zero")
        if self.fee_apr_pct>100000 or self.volatility_pct>100000: raise PoolInputError("APR or volatility input is outside supported bounds")
        if any(getattr(self,k)>100 for k in ("depeg_risk_pct","contract_risk_pct","correlation_risk_pct")): raise PoolInputError("risk percentages must be between 0 and 100")
        if not all(isinstance(getattr(self,k),str) and getattr(self,k).strip() for k in ("pool_id","chain","dex","token0","token1")): raise PoolInputError("pool identity fields must not be empty")
        if self.token0.upper()==self.token1.upper(): raise PoolInputError("pool tokens must differ")
        if self.category not in {"stable_stable","correlated_volatile","uncorrelated_volatile"}: raise PoolInputError("unsupported pool category")

def evaluate_pool(pool: PoolCandidate) -> dict:
    """Heuristic score with hard risk gates; no ML or profitability claim."""
    pool.validate(); ratio=pool.volume_24h_usd/pool.tvl_usd
    gross=min(pool.fee_apr_pct,ratio*Decimal("36500"))
    category_penalty={"stable_stable":Decimal("5"),"correlated_volatile":Decimal("15"),"uncorrelated_volatile":Decimal("30")}[pool.category]
    penalty=pool.volatility_pct*Decimal("0.35")+pool.depeg_risk_pct*Decimal("0.8")+pool.contract_risk_pct*Decimal("1.2")+pool.correlation_risk_pct*Decimal("0.15")+category_penalty
    net=gross-penalty; blocked=[]
    if pool.tvl_usd<100000: blocked.append("pool TVL below $100,000 minimum")
    if pool.volume_24h_usd<=0: blocked.append("no reported 24h volume")
    if pool.contract_risk_pct>=80: blocked.append("contract risk at or above 80/100")
    if pool.depeg_risk_pct>=80: blocked.append("depeg risk at or above 80/100")
    if pool.gas_cost_usd>=100: blocked.append("estimated gas cost at or above $100")
    score=max(Decimal(0),min(Decimal(100),Decimal(50)+net))
    if blocked: score=Decimal(0)
    return {**asdict(pool),"metrics":{"volume_to_tvl":str(ratio),"gross_yield_proxy_pct":str(gross),"risk_penalty_points":str(penalty),"net_yield_proxy_pct":str(net),"score":int(score)},"eligible":not blocked,"blocked_reasons":blocked,"warnings":["Heuristic screening only; not an expected-return forecast.","Impermanent loss, price impact, incentives, taxes and protocol mechanics are not fully modeled."]}

def rank_pools(pools: Iterable[PoolCandidate]) -> list[dict]:
    return sorted((evaluate_pool(p) for p in pools),key=lambda x:(not x["eligible"],-x["metrics"]["score"],x["pool_id"]))

def recommend_allocation(pools: Iterable[PoolCandidate],capital_usd:Decimal|str|int,max_pool_fraction:Decimal|str|int=Decimal("0.25"))->dict:
    try: capital=Decimal(str(capital_usd)); cap=Decimal(str(max_pool_fraction))
    except (InvalidOperation,ValueError): raise PoolInputError("capital and max_pool_fraction must be valid decimals") from None
    if not capital.is_finite() or capital<=0: raise PoolInputError("capital_usd must be positive and finite")
    if not cap.is_finite() or not Decimal(0)<cap<=Decimal(1): raise PoolInputError("max_pool_fraction must be greater than 0 and at most 1")
    ranked=[p for p in rank_pools(pools) if p["eligible"] and p["metrics"]["score"]>0]
    remaining=capital; allocations=[]
    for p in ranked:
        amount=min(capital*cap,remaining)
        if amount<=0: break
        allocations.append({"pool_id":p["pool_id"],"chain":p["chain"],"dex":p["dex"],"allocation_usd":str(amount),"score":p["metrics"]["score"]}); remaining-=amount
    return {"mode":"recommendation_only","capital_usd":str(capital),"allocated_usd":str(capital-remaining),"unallocated_usd":str(remaining),"allocations":allocations,"disclaimer":"No on-chain transaction was prepared or submitted."}
