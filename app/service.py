"""Account-scoped platform service. Every method takes the authenticated ``user_id`` and can only touch that
user's data; resources that belong to someone else are reported as not found. This is the only layer the API
talks to, so every business rule (limits, ownership, simulation gating) lives here."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .accounts import AccountStore
from .config import Settings
from .dex import (
    FixturePoolProvider, PoolSelectionError, PoolSelector, PoolSnapshot, SelectorConfig, SnapshotStore,
    evaluate_constant_product_position, v2_il,
)
from .dex.amm import AMMModelError
from .fx import ConversionQuotes, RateService, StaticRateSource
from .ledger import InvalidOperation, InvalidTransition, LimitExceeded, WalletLedger
from .portfolio import LiquidityPositions, PortfolioManager

MICRO = 1_000_000
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")
CURRENCIES = ("NGN", "USDT")
MAX_AMOUNT_MINOR = 10 ** 13


class NotFound(LookupError):
    pass


class SimulationDisabled(PermissionError):
    pass


class PlatformService:
    def __init__(self, settings: Settings | None = None, *, clock: Callable[[], float] = time.time,
                 rate_service: RateService | None = None, pool_provider: Any = None) -> None:
        self.settings = settings or Settings()
        s = self.settings
        if s.database != ":memory:":
            Path(s.database).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.ledger = WalletLedger(s.database)
        self.accounts = AccountStore(
            self.ledger, password_iterations=s.password_iterations, session_ttl=s.session_ttl_seconds,
            session_idle=s.session_idle_seconds, max_sessions=s.max_sessions_per_user,
            login_max_failures=s.login_max_failures, login_lock_seconds=s.login_lock_seconds, clock=clock)
        self.rates = rate_service or RateService(
            [StaticRateSource(s.fx_ngn_per_usdt, s.fx_spread_bps, name="simulation-static", clock=clock)],
            max_age_seconds=60, clock=clock)
        self.quotes = ConversionQuotes(self.ledger, s.fx_ngn_per_usdt, s.fx_fee_bps, s.quote_ttl_seconds, self.rates)
        self.positions = LiquidityPositions(self.ledger, clock=clock)
        self.manager = PortfolioManager(self.ledger, self.quotes, self.positions, self.rates)
        self.store = SnapshotStore(self.ledger.db, self.ledger.lock)
        self.provider = pool_provider or FixturePoolProvider(s.pool_snapshot_path, clock=clock)
        self.selector_config = SelectorConfig()
        self._deposit_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._last_refresh = 0.0

    def close(self) -> None:
        self.ledger.close()

    def _operation_exists(self, key: str) -> bool:
        with self.ledger.lock:
            return self.ledger.db.execute("SELECT 1 FROM operations WHERE idempotency_key = ?", (key,)).fetchone() is not None

    # ---- validation helpers ------------------------------------------------

    @staticmethod
    def _key(user_id: str, kind: str, client_key: Any) -> str:
        if not isinstance(client_key, str) or not KEY_PATTERN.fullmatch(client_key):
            raise InvalidOperation("Idempotency-Key must be 8-100 characters: letters, digits, '_', '.', ':' or '-'")
        return f"{kind}:{user_id}:{client_key}"

    @staticmethod
    def _amount(value: Any, name: str = "amount_minor") -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_AMOUNT_MINOR:
            raise InvalidOperation(f"{name} must be a whole number of minor units between 1 and {MAX_AMOUNT_MINOR}")
        return value

    @staticmethod
    def _currency(value: Any) -> str:
        if value not in CURRENCIES:
            raise InvalidOperation("currency must be NGN or USDT")
        return value

    def _require_simulation(self) -> None:
        if not self.settings.simulation_mode:
            raise SimulationDisabled("simulated operations are disabled")

    def session_info(self) -> dict[str, Any]:
        s = self.settings
        return {"simulation": s.simulation_mode, "sim_time_scale": s.sim_time_scale,
                "fx_fee_bps": s.fx_fee_bps, "quote_ttl_seconds": s.quote_ttl_seconds,
                "deposit_limits": {"per_request": dict(s.sim_deposit_max), "per_day": dict(s.sim_deposit_daily)},
                "withdrawal_limits": {c: vars(l) for c, l in self.ledger.withdrawal_limits.items()},
                "min_position_minor": int(self.selector_config.min_position_usd * MICRO),
                "pool_data": "synthetic example data, not market data"}

    # ---- wallet ---------------------------------------------------------------

    def wallet(self, user_id: str) -> dict[str, Any]:
        value = self.manager.valuation(user_id)
        value.pop("positions", None)
        return value

    def history(self, user_id: str, limit: int = 50, before: int | None = None) -> list[dict[str, Any]]:
        return self.ledger.history(user_id, limit, before)

    def deposit(self, user_id: str, currency: Any, amount_minor: Any, client_key: Any) -> dict[str, Any]:
        self._require_simulation()
        currency, amount = self._currency(currency), self._amount(amount_minor)
        key = self._key(user_id, "dep", client_key)
        per_request, per_day = dict(self.settings.sim_deposit_max)[currency], dict(self.settings.sim_deposit_daily)[currency]
        with self._deposit_lock:     # serialises check-then-credit so concurrent requests cannot exceed the cap
            existing = self._operation_exists(key)
            if not existing:
                if amount > per_request:
                    raise LimitExceeded(f"simulated deposits are limited to {per_request} per request")
                if self.ledger.daily_total(user_id, currency, "simulated deposit") + amount > per_day:
                    raise LimitExceeded(f"simulated deposits are limited to {per_day} per day")
            result = self.ledger.deposit(user_id, currency, amount, key)
        if not existing:
            self.accounts.audit(user_id, "deposit_simulated", currency=currency, amount_minor=amount)
        return result

    # ---- withdrawals ----------------------------------------------------------

    def request_withdrawal(self, user_id: str, currency: Any, amount_minor: Any, destination: Any,
                           client_key: Any) -> dict[str, Any]:
        currency, amount = self._currency(currency), self._amount(amount_minor)
        result = self.ledger.request_withdrawal(user_id, currency, amount, destination,
                                                self._key(user_id, "wd", client_key))
        self.accounts.audit(user_id, "withdrawal_requested", currency=currency, amount_minor=amount,
                            withdrawal_id=result["withdrawal_id"])
        return result

    def withdrawals(self, user_id: str) -> list[dict[str, Any]]:
        return list(reversed(self.ledger.list_withdrawals(user_id)))

    def _owned_withdrawal(self, user_id: str, withdrawal_id: Any) -> dict[str, Any]:
        try:
            row = self.ledger.get_withdrawal(withdrawal_id) if isinstance(withdrawal_id, str) else None
        except InvalidOperation:
            row = None
        if row is None or row["account"] != user_id:
            raise NotFound("withdrawal not found")
        return row

    def cancel_withdrawal(self, user_id: str, withdrawal_id: Any) -> dict[str, Any]:
        row = self._owned_withdrawal(user_id, withdrawal_id)
        result = self.ledger.cancel_withdrawal(row["withdrawal_id"], "cancelled by customer")
        self.accounts.audit(user_id, "withdrawal_cancelled", withdrawal_id=row["withdrawal_id"])
        return result

    def simulate_payout(self, user_id: str, withdrawal_id: Any, outcome: Any) -> dict[str, Any]:
        """Stand in for the payout provider: move a pending/processing withdrawal to completed or failed."""
        self._require_simulation()
        if outcome not in ("complete", "fail"):
            raise InvalidOperation("outcome must be 'complete' or 'fail'")
        row = self._owned_withdrawal(user_id, withdrawal_id)
        wid = row["withdrawal_id"]
        if row["status"] == "pending":
            self.ledger.mark_withdrawal_processing(wid, f"sim-{wid[-12:]}")
        elif row["status"] != "processing":
            raise InvalidTransition(f"withdrawal is {row['status']}")
        result = (self.ledger.complete_withdrawal(wid) if outcome == "complete"
                  else self.ledger.fail_withdrawal(wid, "simulated provider failure"))
        self.accounts.audit(user_id, f"withdrawal_{result['status']}", withdrawal_id=wid)
        return result

    # ---- conversion -----------------------------------------------------------

    def create_quote(self, user_id: str, source_currency: Any, amount_minor: Any) -> dict[str, Any]:
        source, amount = self._currency(source_currency), self._amount(amount_minor)
        quote = self.quotes.create_quote(user_id, source, amount, now=self.clock())
        quote.pop("account", None)
        quote["ttl_seconds"] = self.settings.quote_ttl_seconds
        quote["server_time"] = self.clock()
        self.accounts.audit(user_id, "quote_created", quote_id=quote["quote_id"], source=source, amount_minor=amount)
        return quote

    def execute_quote(self, user_id: str, quote_id: Any, client_key: Any) -> dict[str, Any]:
        if not isinstance(quote_id, str) or len(quote_id) > 64:
            raise NotFound("quote not found")
        result = self.quotes.execute(quote_id, self._key(user_id, "conv", client_key), account=user_id, now=self.clock())
        self.accounts.audit(user_id, "conversion_executed", quote_id=quote_id)
        return result

    # ---- pools ------------------------------------------------------------------

    def _snapshots(self) -> list[PoolSnapshot]:
        now = self.clock()
        with self._refresh_lock:
            if now - self._last_refresh >= self.settings.pool_refresh_seconds or not self._last_refresh:
                for snapshot in self.provider.fetch_pools():
                    self.store.save(snapshot)
                self._last_refresh = now
        return self.store.latest_all()

    def _selector(self) -> tuple[PoolSelector, dict[str, PoolSnapshot]]:
        snapshots = self._snapshots()
        selector = PoolSelector(self.selector_config)
        selector.add_pools(snapshots)
        return selector, {s.pool_id: s for s in snapshots}

    def pools(self) -> list[dict[str, Any]]:
        selector, by_id = self._selector()
        now, cfg, out = self.clock(), self.selector_config, []
        for snapshot in selector.pools:
            ev = selector.evaluate(snapshot, now).to_dict()
            ev.update(snapshot_as_of=snapshot.as_of, source=snapshot.source, dex=snapshot.dex,
                      fee_tier_bps=snapshot.fee_tier_bps, price=snapshot.latest_price,
                      max_position_minor=int(cfg.max_pool_tvl_share * snapshot.tvl_usd * MICRO))
            out.append(ev)
        out.sort(key=lambda e: (not e["eligible"], -e["score"], e["pool_id"]))
        return out

    def recommendation(self, amount_minor: Any) -> dict[str, Any]:
        amount = self._amount(amount_minor)
        selector, _ = self._selector()
        try:
            result = selector.select(amount, now=self.clock())
        except PoolSelectionError as error:
            raise InvalidOperation(str(error)) from None
        evaluations = {e.pool_id: e for e in result.evaluations}
        items = [{"pool_id": pid, "amount_minor": amt, "tokens": list(evaluations[pid].tokens),
                  "chain": evaluations[pid].chain, "category": evaluations[pid].category.value,
                  "net_apr": evaluations[pid].net_apr, "risk_score": evaluations[pid].risk_score,
                  "net_apr_after_gas": result.allocation.details[pid]["net_apr_after_gas"]}
                 for pid, amt in sorted(result.allocation.allocations.items(), key=lambda kv: -kv[1])]
        return {"allocations": items, "reserve_minor": result.allocation.reserve_minor,
                "unallocated_minor": result.allocation.unallocated_minor, "excluded": result.excluded,
                "rationale": result.allocation.rationale,
                "disclaimer": "Screening aid on synthetic data; not a forecast or advice. Nothing is executed."}

    def scenario(self, pool_id: Any, price_change_pct: float, days: int = 30, amount_usdt: float = 1000.0) -> dict[str, Any]:
        selector, by_id = self._selector()
        snapshot = by_id.get(pool_id) if isinstance(pool_id, str) else None
        if snapshot is None or not snapshot.latest_price:
            raise NotFound("pool not found")
        ev = selector.evaluate(snapshot, self.clock())
        if not -90 <= price_change_pct <= 1000 or not 1 <= days <= 365:
            raise InvalidOperation("price_change_pct must be -90..1000 and days 1..365")
        price0 = snapshot.latest_price
        fees = amount_usdt * ev.fee_apr * days / 365.0
        try:
            result = evaluate_constant_product_position(
                str(amount_usdt / 2 / price0), str(amount_usdt / 2), str(price0 * (1 + price_change_pct / 100.0)),
                accrued_fees_quote=str(fees))
        except AMMModelError as error:
            raise InvalidOperation(str(error)) from None
        return {"pool_id": pool_id, "assumed_deposit_usdt": amount_usdt, "price_change_pct": price_change_pct,
                "days": days, "impermanent_loss_pct": float(result.impermanent_loss_pct),
                "hold_value": float(result.hold_value_quote), "lp_value_before_fees": float(result.lp_value_before_fees_quote),
                "modelled_fees": fees, "net_vs_hold": float(result.net_pnl_vs_hold_quote),
                "warnings": list(result.warnings) + ["Fees use the pool's modelled fee APR and assume constant volume."]}

    # ---- positions ----------------------------------------------------------------

    def _pool_capacity(self, snapshot: PoolSnapshot, amount: int) -> None:
        cap = int(self.selector_config.max_pool_tvl_share * snapshot.tvl_usd * MICRO)
        used = self.positions.pool_exposure(snapshot.pool_id)
        if used + amount > cap:
            raise LimitExceeded(f"pool capacity reached: at most {cap} micro-USDT in total ({used} already placed)")

    def open_position(self, user_id: str, pool_id: Any, amount_minor: Any, client_key: Any) -> dict[str, Any]:
        amount, key = self._amount(amount_minor), self._key(user_id, "pos", client_key)
        selector, by_id = self._selector()
        snapshot = by_id.get(pool_id) if isinstance(pool_id, str) else None
        if snapshot is None:
            raise NotFound("pool not found")
        existing = self._operation_exists(key)
        if not existing:
            evaluation = selector.evaluate(snapshot, self.clock())
            if not evaluation.eligible:
                raise InvalidOperation("pool is not eligible: " + "; ".join(evaluation.reasons))
            if amount < int(self.selector_config.min_position_usd * MICRO):
                raise InvalidOperation(f"minimum position is {self.selector_config.min_position_usd:g} USDT")
            drag = selector.gas_drag_apr(snapshot.chain, amount / MICRO)
            if evaluation.net_apr - drag < self.selector_config.category_limits[evaluation.category].min_net_apr:
                raise InvalidOperation(f"gas on {snapshot.chain} would consume the yield at this size")
            if self.positions.count_open(user_id) >= self.settings.max_open_positions:
                raise LimitExceeded(f"at most {self.settings.max_open_positions} open positions")
            self._pool_capacity(snapshot, amount)
        result = self.positions.open_position(user_id, snapshot.pool_id, snapshot.token0, snapshot.token1, amount, key,
                                              entry_price=snapshot.latest_price)
        if not existing:
            self.accounts.audit(user_id, "position_opened", pool_id=snapshot.pool_id, amount_minor=amount,
                                position_id=result["position_id"])
        return result

    def deploy_recommendation(self, user_id: str, amount_minor: Any, client_key: Any) -> list[dict[str, Any]]:
        amount, key = self._amount(amount_minor), self._key(user_id, "deploy", client_key)
        selector, by_id = self._selector()
        result = selector.select(amount, now=self.clock())
        if not result.allocation.allocations:
            raise InvalidOperation("no eligible pools at this size")
        if self.positions.count_open(user_id) + len(result.allocation.allocations) > self.settings.max_open_positions:
            raise LimitExceeded(f"at most {self.settings.max_open_positions} open positions")
        for pool_id, amt in result.allocation.allocations.items():
            self._pool_capacity(by_id[pool_id], amt)
        opened = self.manager.deploy(user_id, result.allocation, key)
        self.accounts.audit(user_id, "allocation_deployed", amount_minor=amount, positions=len(opened))
        return opened

    def _mark(self, position: dict[str, Any], by_id: dict[str, PoolSnapshot], selector: PoolSelector, force: bool) -> None:
        """Value an open position with the simulation model: impermanent loss from the pool price change plus
        fees accrued at the pool's modelled APR over simulated time. The customer never supplies a valuation."""
        snapshot = by_id.get(position["pool_id"])
        if snapshot is None or position["status"] == "closed":
            return
        now = self.clock()
        with self.ledger.lock:
            last = self.ledger.db.execute("SELECT MAX(as_of) FROM position_marks WHERE position_id = ?",
                                          (position["position_id"],)).fetchone()[0]
        if not force and last is not None and now - last < self.settings.min_mark_interval_seconds:
            return
        principal = position["principal_minor"]
        entry, price = position.get("entry_price"), snapshot.latest_price
        value = principal * (1.0 + (v2_il(price / entry) if entry and price else 0.0))
        elapsed = max(0.0, now - (position.get("opened_ts") or now)) * self.settings.sim_time_scale
        fees = principal * selector.evaluate(snapshot, now).fee_apr * elapsed / (365.0 * 86_400)
        room = principal * self.positions.max_mark_multiple - value
        fees = max(int(min(fees, max(room, 0.0))), position["accrued_fees_minor"])
        value = int(min(value, principal * self.positions.max_mark_multiple - fees))
        self.positions.mark_to_market(position["position_id"], max(value, 0), fees, source="simulation-model",
                                      as_of=max(now, last or 0.0))

    def list_positions(self, user_id: str) -> list[dict[str, Any]]:
        selector, by_id = self._selector()
        for position in self.positions.list_positions(user_id):
            if position["status"] != "closed":
                self._mark(position, by_id, selector, force=False)
        out = []
        for p in self.positions.list_positions(user_id):
            pnl = p["realized_pnl_minor"] if p["status"] == "closed" else p["unrealized_pnl_usdt_micro"]
            out.append({"position_id": p["position_id"], "pool_id": p["pool_id"], "pair": f"{p['asset']}/{p['quote_asset']}",
                        "status": p["status"], "principal_minor": p["principal_minor"],
                        "value_minor": p["closed_value_minor"] if p["status"] == "closed" else p["current_value_minor"] + p["accrued_fees_minor"],
                        "fees_minor": p["accrued_fees_minor"], "pnl_minor": pnl or 0, "opened_ts": p["opened_ts"],
                        "simulated_days": round(max(0.0, self.clock() - (p["opened_ts"] or self.clock()))
                                                * self.settings.sim_time_scale / 86_400, 2)})
        return out

    def close_position(self, user_id: str, position_id: Any, client_key: Any) -> dict[str, Any]:
        key = self._key(user_id, "close", client_key)
        position = self.positions.get_position(position_id if isinstance(position_id, str) else "", account=user_id)
        if position["status"] != "closed":
            selector, by_id = self._selector()
            self._mark(position, by_id, selector, force=True)
        result = self.positions.close_position(position["position_id"], key, account=user_id)
        self.accounts.audit(user_id, "position_closed", position_id=position["position_id"],
                            returned_minor=result["returned_usdt_micro"])
        return result

    def activity(self, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return self.accounts.recent_audit(user_id, limit)
