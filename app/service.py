"""Account-scoped platform service. Every method takes the authenticated ``user_id`` and can only touch that
user's data; resources that belong to someone else are reported as not found. This is the only layer the API
talks to, so every business rule (limits, ownership, simulation gating) lives here."""
from __future__ import annotations

import contextvars
import errno
import hashlib
import logging
import math
import os
import re
import secrets
import stat
import threading
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .accounts import AccountStore, ConsoleMailer, KycStore, SmtpMailer
from .config import Settings
from .dex.defillama import DefiLlamaPoolProvider, PoolUniverse
from .dex.http import ProviderError
from .dex import (
    FixturePoolProvider, PoolSelectionError, PoolSelector, PoolSnapshot, SelectorConfig, SnapshotStore,
    evaluate_constant_product_position, v2_il,
)
from .dex.amm import AMMModelError
from .fx import ConversionQuotes, RateService, RateUnavailable, StaticRateSource, build_rate_service
from .live import WebhookRejected, build_adapters
from .ledger import InvalidOperation, InvalidTransition, LimitExceeded, WalletLedger
from .payouts import AmbiguousBank, BankDirectory, PayoutAccounts, SimulatedNameEnquiry
from .portfolio import LiquidityPositions, PortfolioManager

MICRO = 1_000_000
log = logging.getLogger("app.service")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")
CURRENCIES = ("NGN", "USDT")
MAX_AMOUNT_MINOR = 10 ** 13


def _validate_secret_directory(directory: Path) -> None:
    """Require a real directory owned by this user and not writable by others."""
    info = os.lstat(directory)

    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError("secret key parent must be a real directory")

    if info.st_uid != os.geteuid():
        raise PermissionError(
            "secret key directory must be owned by the application user"
        )

    if stat.S_IMODE(info.st_mode) & 0o022:
        raise PermissionError(
            f"secret key directory is writable by group or others; "
            f"run chmod go-w {directory}"
        )


def _fsync_directory(directory: Path) -> None:
    """Persist directory-entry changes after atomically creating the secret."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_secret(path: Path | None) -> bytes:
    """Load a persistent owner-only server secret, creating it atomically."""
    if path is None:
        return secrets.token_bytes(32)

    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_secret_directory(path.parent)

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )

    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        data = secrets.token_bytes(32)

        try:
            os.fchmod(descriptor, 0o600)

            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

            try:
                os.link(temporary_path, path, follow_symlinks=False)
            except FileExistsError:
                pass
            else:
                _fsync_directory(path.parent)
                return data
        finally:
            temporary_path.unlink(missing_ok=True)

        # Another process created the secret first.
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PermissionError(
                    "secret key path must not be a symbolic link"
                ) from exc
            raise

    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PermissionError(
                "secret key path must not be a symbolic link"
            ) from exc
        raise

    try:
        info = os.fstat(descriptor)

        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("secret key must be a regular file")

        if info.st_uid != os.geteuid():
            raise PermissionError(
                "secret key must be owned by the application user"
            )

        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(
                f"secret key permissions are too broad; run chmod 600 {path}"
            )

        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(33)

    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise

    if len(data) != 32:
        raise RuntimeError(
            "secret key file must contain exactly 32 bytes; "
            "restore the original key or rotate affected credentials"
        )

    return data


LIVE_TERMS_VERSION = "2026-10-27"
LIVE_TERMS = (
    "Live mode uses the payment and chain rails the operator has configured. Money there is not a simulation.",
    "Providing liquidity can lose value: impermanent loss, token price moves, depegs, and smart-contract failures.",
    "Returns shown for demo mode are paper results and say nothing about what live mode will earn.",
    "Nothing here is investment, legal or tax advice. The operator must replace these terms with approved ones.",
)
MIN_LIVE_DEPOSIT = 100_000                  # NGN 1,000 in kobo


@dataclass
class Book:
    """One set of money books. Demo and live never share a ledger, quote table or position table."""
    ledger: WalletLedger
    quotes: ConversionQuotes
    positions: LiquidityPositions
    manager: PortfolioManager
    rates: RateService


class NoRateSource:
    """Live book with no configured rate source: every quote fails instead of using a placeholder rate."""
    name = "none"

    def fetch(self) -> Any:
        raise RateUnavailable("no live rate source is configured")


class LiveUnavailable(PermissionError):
    def __init__(self, requirements: list[dict[str, Any]], message: str = "live mode is not available yet") -> None:
        super().__init__(message)
        self.requirements = requirements


class NotFound(LookupError):
    pass


class SimulationDisabled(PermissionError):
    pass


class PlatformService:
    def __init__(self, settings: Settings | None = None, *, clock: Callable[[], float] = time.time,
                 rate_service: RateService | None = None, pool_provider: Any = None, mailer: Any = None,
                 name_enquiry: Any = None, rails: Any = None, chain: Any = None,
                 live_rate_service: RateService | None = None) -> None:
        self.settings = settings or Settings()
        s = self.settings
        memory = s.database == ":memory:"
        if not memory:
            Path(s.database).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        demo_ledger = WalletLedger(s.database)
        self.platform = demo_ledger        # users, sessions, KYC, payout accounts and pool snapshots live beside the demo books
        outbox = None if memory else Path(s.database).parent / "outbox.log"
        self.accounts = AccountStore(
            demo_ledger, password_iterations=s.password_iterations, session_ttl=s.session_ttl_seconds,
            session_idle=s.session_idle_seconds, max_sessions=s.max_sessions_per_user,
            login_max_failures=s.login_max_failures, login_lock_seconds=s.login_lock_seconds, clock=clock,
            pin_pepper=load_secret(None if memory else Path(s.secret_key_path or Path(s.database).parent / "secret.key")),
            mailer=mailer or (SmtpMailer(s.smtp_host, s.smtp_port, s.smtp_from, s.smtp_user, s.smtp_password)
                              if s.smtp_host else ConsoleMailer(outbox)))
        self._mode: contextvars.ContextVar[str] = contextvars.ContextVar(f"funds_mode_{id(self)}", default="demo")
        self.accounts.audit_context = lambda: {"mode": self._mode.get()}
        self.kyc = KycStore(demo_ledger, clock=clock)
        self.banks = BankDirectory.load(s.banks_path) if s.banks_path else BankDirectory.load()
        self.payouts = PayoutAccounts(demo_ledger, self.banks, name_enquiry or SimulatedNameEnquiry(self.banks))
        demo_rates = rate_service or (build_rate_service(s.rate_sources, s.fx_spread_bps) if s.rate_sources else RateService(
            [StaticRateSource(s.fx_ngn_per_usdt, s.fx_spread_bps, name="simulation-static", clock=clock)],
            max_age_seconds=60, clock=clock))
        self.books = {"demo": self._make_book(demo_ledger, demo_rates)}
        self.rails, self.chain = (rails, chain) if rails or chain else build_adapters(s.live_rails, s.webhook_secret)
        self.live_healthy = False
        if s.live_enabled:
            live_path = ":memory:" if memory else s.live_database or str(Path(s.database).parent / "live_ledger.sqlite3")
            live_ledger = WalletLedger(live_path)
            live_rates = live_rate_service or (build_rate_service(s.rate_sources, s.fx_spread_bps, min_sources=2) if s.rate_sources
                          else RateService([NoRateSource()], max_age_seconds=60, clock=clock))
            self.books["live"] = self._make_book(live_ledger, live_rates)
            with live_ledger.transaction():
                live_ledger.db.execute("CREATE TABLE IF NOT EXISTS webhook_events (event_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
                                       "received_at REAL NOT NULL)")
            self.live_healthy = live_ledger.reconcile()["ok"]
        with demo_ledger.transaction():
            demo_ledger.db.execute("CREATE TABLE IF NOT EXISTS live_access (user_id TEXT PRIMARY KEY, terms_version TEXT NOT NULL, "
                                   "accepted_at REAL NOT NULL)")
        self.store = SnapshotStore(demo_ledger.db, demo_ledger.lock)
        if pool_provider is None:
            if s.data_source == "live":
                defaults = PoolUniverse()
                pool_provider = DefiLlamaPoolProvider(
                    PoolUniverse(chains=s.pool_chains or defaults.chains, projects=s.pool_projects or defaults.projects), clock=clock)
            else:
                pool_provider = FixturePoolProvider(s.pool_snapshot_path, clock=clock)
        self.provider = pool_provider
        self.data_status: dict[str, Any] = {"ok": True, "error": "", "as_of": None}
        self.selector_config = SelectorConfig()
        self._deposit_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._last_refresh = 0.0

    def _make_book(self, ledger: WalletLedger, rates: RateService) -> Book:
        s = self.settings
        quotes = ConversionQuotes(ledger, s.fx_ngn_per_usdt, s.fx_fee_bps, s.quote_ttl_seconds, rates)
        positions = LiquidityPositions(ledger, clock=self.clock)
        return Book(ledger, quotes, positions, PortfolioManager(ledger, quotes, positions, rates), rates)

    # ---- funds mode: every money method reads the current book ---------------------------

    @property
    def book(self) -> Book:
        return self.books[self._mode.get()]

    @property
    def ledger(self) -> WalletLedger:
        return self.book.ledger

    @property
    def quotes(self) -> ConversionQuotes:
        return self.book.quotes

    @property
    def positions(self) -> LiquidityPositions:
        return self.book.positions

    @property
    def manager(self) -> PortfolioManager:
        return self.book.manager

    @property
    def rates(self) -> RateService:
        return self.book.rates

    @property
    def mode(self) -> str:
        return self._mode.get()

    @contextmanager
    def use_mode(self, mode: Any, user_id: str | None = None):
        """Run a block against the demo or live books. Live mode is refused unless every requirement is met."""
        if mode not in ("demo", "live"):
            raise InvalidOperation("funds mode must be 'demo' or 'live'")
        if mode == "live":
            self._require_live(user_id)
        token = self._mode.set(mode)
        try:
            yield
        finally:
            self._mode.reset(token)

    def live_requirements(self, user_id: str | None = None) -> list[dict[str, Any]]:
        s = self.settings
        reqs = [("enabled", s.live_enabled and "live" in self.books, "Live mode is not enabled on this server."),
                ("not_paused", not s.live_paused, "Live mode is paused by the operator."),
                ("rails", self.rails is not None and self.chain is not None, "No payment or chain rails are configured."),
                ("healthy", self.live_healthy, "The live ledger has not passed its integrity check.")]
        if user_id:
            with self.platform.lock:
                accepted = self.platform.db.execute("SELECT 1 FROM live_access WHERE user_id = ? AND terms_version = ?",
                                                    (user_id, LIVE_TERMS_VERSION)).fetchone() is not None
            status = self.accounts.security_status(user_id)
            reqs += [("kyc", self.kyc.tier(user_id) >= 1, "Complete identity verification (tier 1)."),
                     ("email", status["email_verified"], "Verify your email address."),
                     ("pin", status["pin"]["set"], "Set your transaction PIN."),
                     ("terms", accepted, "Accept the live-mode terms.")]
        return [{"key": k, "ok": bool(ok), "message": m} for k, ok, m in reqs]

    def _require_live(self, user_id: str | None) -> None:
        requirements = self.live_requirements(user_id)
        if not user_id:
            requirements = [r for r in requirements if r["key"] in ("enabled", "not_paused", "rails", "healthy")]
        if any(not r["ok"] for r in requirements):
            raise LiveUnavailable(requirements)

    def live_status(self, user_id: str) -> dict[str, Any]:
        reqs = self.live_requirements(user_id)
        sandbox = bool((self.rails and self.rails.sandbox) or (self.chain and self.chain.sandbox))
        return {"enabled": self.settings.live_enabled, "available": all(r["ok"] for r in reqs), "requirements": reqs,
                "rails": {"payments": getattr(self.rails, "name", None), "chain": getattr(self.chain, "name", None), "sandbox": sandbox},
                "terms_version": LIVE_TERMS_VERSION, "terms": list(LIVE_TERMS)}

    def accept_live_terms(self, user_id: str, pin: Any, ip: str = "") -> dict[str, Any]:
        self._authorize(user_id, pin, ip)
        with self.platform.transaction():
            self.platform.db.execute("INSERT INTO live_access(user_id, terms_version, accepted_at) VALUES (?, ?, ?) "
                                     "ON CONFLICT(user_id) DO UPDATE SET terms_version = excluded.terms_version, accepted_at = excluded.accepted_at",
                                     (user_id, LIVE_TERMS_VERSION, self.clock()))
        self.accounts.audit(user_id, "live_terms_accepted", ip, terms_version=LIVE_TERMS_VERSION)
        return self.live_status(user_id)

    def close(self) -> None:
        for book in self.books.values():
            book.ledger.close()

    def _authorize(self, user_id: str, pin: Any, ip: str = "", key: str | None = None) -> None:
        """Check the transaction PIN. A replay of an already-recorded operation needs no PIN (it changes nothing)."""
        if not self.settings.require_pin or (key is not None and self._operation_exists(key)):
            return
        self.accounts.verify_pin(user_id, pin, ip)

    def _prefix_exists(self, prefix: str) -> bool:
        with self.ledger.lock:
            return self.ledger.db.execute("SELECT 1 FROM operations WHERE idempotency_key LIKE ? ESCAPE '\\' LIMIT 1",
                                          (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)).fetchone() is not None

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
        if self._mode.get() != "demo":
            raise SimulationDisabled("simulated deposits and payouts exist only in demo mode")

    def session_info(self) -> dict[str, Any]:
        s = self.settings
        return {"simulation": s.simulation_mode, "sim_time_scale": s.time_scale, "data_source": s.data_source, "data_status": self.data_status,
                "fx_fee_bps": s.fx_fee_bps, "quote_ttl_seconds": s.quote_ttl_seconds,
                "deposit_limits": {"per_request": dict(s.sim_deposit_max), "per_day": dict(s.sim_deposit_daily)},
                "withdrawal_limits": {c: vars(l) for c, l in self.ledger.withdrawal_limits.items()},
                "min_position_minor": int(self.selector_config.min_position_usd * MICRO),
                "pool_data": ("synthetic example data, not market data" if s.data_source == "fixture" else
                              "live pool data from DefiLlama; positions are paper-traded with demo funds and nothing is executed on-chain"),
                "name_enquiry": "simulated (account names are invented)",
                "live": {"enabled": s.live_enabled, "sandbox": bool((self.rails and self.rails.sandbox) or (self.chain and self.chain.sandbox))}}

    # ---- wallet ---------------------------------------------------------------

    def wallet(self, user_id: str) -> dict[str, Any]:
        value = self.manager.valuation(user_id)
        value.pop("positions", None)
        return value

    def history(self, user_id: str, limit: int = 50, before: int | None = None) -> list[dict[str, Any]]:
        return self.ledger.history(user_id, limit, before)

    def deposit(self, user_id: str, currency: Any, amount_minor: Any, client_key: Any, pin: Any = None,
                ip: str = "") -> dict[str, Any]:
        self._require_simulation()
        currency, amount = self._currency(currency), self._amount(amount_minor)
        key = self._key(user_id, "dep", client_key)
        self._authorize(user_id, pin, ip, key)
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

    # ---- live deposits and webhooks -------------------------------------------------------

    def live_deposit_request(self, user_id: str, amount_minor: Any, client_key: Any, pin: Any = None, ip: str = "") -> dict[str, Any]:
        """Ask the payment rail where to send an NGN deposit. Funds are credited only when the signed webhook arrives."""
        with self.use_mode("live", user_id):
            amount = self._amount(amount_minor)
            if amount < MIN_LIVE_DEPOSIT:
                raise InvalidOperation("the minimum deposit is NGN 1,000")
            key = self._key(user_id, "ldep", client_key)
            self._authorize(user_id, pin, ip)
            reference = "D" + hashlib.sha256(key.encode()).hexdigest()[:20].upper()
            row = self.ledger.register_deposit(user_id, "NGN", amount, reference)
            instructions = self.rails.deposit_instructions(user_id, reference, amount)
            self.accounts.audit(user_id, "live_deposit_requested", ip, reference=reference, amount_minor=amount)
            return {"reference": reference, "status": row["status"], "instructions": instructions, "sandbox": self.rails.sandbox}

    def handle_webhook(self, source: str, headers: Any, raw: bytes) -> dict[str, Any]:
        """Apply a signed provider event to the live books. Idempotent: replays change nothing."""
        adapter = {"payments": self.rails, "chain": self.chain}.get(source)
        if adapter is None or "live" not in self.books:
            raise NotFound("unknown webhook")
        event = adapter.verify_webhook(headers, raw, self.clock())
        with self.use_mode("live"):
            with self.ledger.lock:
                if self.ledger.db.execute("SELECT 1 FROM webhook_events WHERE event_id = ?", (event["id"],)).fetchone():
                    return {"status": "duplicate"}
            outcome = self._apply_event(source, event)
            with self.ledger.lock:
                self.ledger.db.execute("INSERT OR IGNORE INTO webhook_events(event_id, source, received_at) VALUES (?, ?, ?)",
                                       (event["id"], source, self.clock()))
        return {"status": outcome}

    def _apply_event(self, source: str, event: dict[str, Any]) -> str:
        kind = event["type"]
        if kind == "deposit.confirmed" and source == "payments":
            reference = event.get("reference")
            with self.ledger.lock:
                row = self.ledger.db.execute("SELECT * FROM deposits WHERE provider_ref = ?",
                                             (reference if isinstance(reference, str) else "",)).fetchone()
            if row is None:
                raise WebhookRejected("unknown deposit reference")
            if (row["amount_minor"], row["currency"]) != (event.get("amount_minor"), event.get("currency")):
                self.accounts.audit(row["account"], "live_deposit_mismatch", reference=reference)
                raise WebhookRejected("amount or currency does not match the registered deposit")
            self.ledger.confirm_deposit(reference)
            self.accounts.audit(row["account"], "live_deposit_confirmed", reference=reference, amount_minor=row["amount_minor"])
            return "applied"
        if kind in ("payout.completed", "payout.failed"):
            wid = event.get("withdrawal_id")
            try:
                row = self.ledger.get_withdrawal(wid) if isinstance(wid, str) else None
            except InvalidOperation:
                row = None
            if row is None or row["currency"] != ("NGN" if source == "payments" else "USDT"):
                raise WebhookRejected("unknown withdrawal for this source")
            done = "completed" if kind == "payout.completed" else "failed"
            if row["status"] == done:
                return "duplicate"
            if row["status"] != "processing":
                raise WebhookRejected(f"withdrawal is {row['status']}")
            if done == "completed":
                self.ledger.complete_withdrawal(wid)
            else:
                self.ledger.fail_withdrawal(wid, str(event.get("reason", "provider reported failure"))[:100])
            self.accounts.audit(row["account"], f"live_withdrawal_{done}", withdrawal_id=wid)
            return "applied"
        raise WebhookRejected("unsupported event type")

    # ---- withdrawals ----------------------------------------------------------

    def request_withdrawal(self, user_id: str, currency: Any, amount_minor: Any, destination: Any, client_key: Any,
                           beneficiary_id: Any = None, pin: Any = None, ip: str = "") -> dict[str, Any]:
        """Request a payout. Give either a saved payout account (``beneficiary_id``) or a ``destination``; for NGN the
        bank code may be omitted when the bank can be confirmed from the account number. Needs the transaction PIN
        and is limited by the user's KYC tier."""
        currency, amount = self._currency(currency), self._amount(amount_minor)
        key = self._key(user_id, "wd", client_key)
        if (beneficiary_id is None) == (destination is None):
            raise InvalidOperation("give either beneficiary_id or destination")
        if beneficiary_id is not None:
            saved = self.payouts.get(user_id, beneficiary_id)
            if saved["currency"] != currency:
                raise InvalidOperation("that payout account is for a different currency")
            destination = saved["destination"]
        if not isinstance(destination, dict):
            raise InvalidOperation("destination must be an object")
        if currency == "NGN":
            full = self.payouts.complete_ngn_destination(destination)
            destination = {"bank_code": full["bank_code"], "account_number": full["account_number"]}
        self._authorize(user_id, pin, ip, key)
        result = self.ledger.request_withdrawal(user_id, currency, amount, destination, key,
                                                limits=self.kyc.limits(user_id, currency))
        if self._mode.get() == "live":
            result = self._dispatch_live_payout(user_id, result)
        self.accounts.audit(user_id, "withdrawal_requested", ip, currency=currency, amount_minor=amount,
                            withdrawal_id=result["withdrawal_id"])
        return self._decorate_withdrawal(result)

    def _dispatch_live_payout(self, user_id: str, withdrawal: dict[str, Any]) -> dict[str, Any]:
        """Claim a withdrawal before dispatch so concurrent requests cannot send it twice."""
        if withdrawal["status"] != "pending":
            return withdrawal

        claimed, current = self.ledger.claim_withdrawal_submission(withdrawal["withdrawal_id"])
        if not claimed:
            return self._decorate_withdrawal(current)

        wid = current["withdrawal_id"]
        adapter = self.rails if current["currency"] == "NGN" else self.chain

        if adapter is None:
            self.ledger.fail_withdrawal(wid, "no payout rail is configured")
            raise ProviderError("no rail is configured for this currency; your funds were returned")

        try:
            external = (
                adapter.send_payout(current)
                if current["currency"] == "NGN"
                else adapter.send_usdt(current)
            )
            if not isinstance(external, str) or not external:
                raise ProviderError("payout adapter returned an invalid provider reference")
        except Exception as error:  # noqa: BLE001 - provider outcome depends on adapter type
            self.accounts.audit(
                user_id, "withdrawal_rail_error",
                withdrawal_id=wid, error=type(error).__name__,
            )

            if getattr(adapter, "sandbox", False):
                self.ledger.fail_withdrawal(wid, "sandbox payout rail error")
                raise ProviderError(
                    "the sandbox payout failed; your funds were returned"
                ) from error

            # A real provider may have accepted the payout before a timeout or
            # connection failure. Keep the funds held for provider reconciliation.
            raise ProviderError(
                "the payout outcome is uncertain; funds remain reserved while it is reconciled"
            ) from error

        return self._decorate_withdrawal(self.ledger.mark_withdrawal_processing(wid, external))

    def _decorate_withdrawal(self, row: dict[str, Any]) -> dict[str, Any]:
        bank = self.banks.get(row["destination"].get("bank_code", "")) if row["currency"] == "NGN" else None
        return {**row, "bank_name": bank.name if bank else ""}

    def withdrawals(self, user_id: str) -> list[dict[str, Any]]:
        return [self._decorate_withdrawal(w) for w in reversed(self.ledger.list_withdrawals(user_id))]

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
        if self._mode.get() == "live" and len(self.rates.sources) < 2:
            raise LiveUnavailable(self.live_requirements(user_id), "live conversions need at least two independent rate sources")
        quote = self.quotes.create_quote(user_id, source, amount, now=self.clock())
        quote.pop("account", None)
        quote["ttl_seconds"] = self.settings.quote_ttl_seconds
        quote["server_time"] = self.clock()
        self.accounts.audit(user_id, "quote_created", quote_id=quote["quote_id"], source=source, amount_minor=amount)
        return quote

    def execute_quote(self, user_id: str, quote_id: Any, client_key: Any, pin: Any = None, ip: str = "") -> dict[str, Any]:
        if not isinstance(quote_id, str) or len(quote_id) > 64:
            raise NotFound("quote not found")
        key = self._key(user_id, "conv", client_key)
        self._authorize(user_id, pin, ip, key)
        result = self.quotes.execute(quote_id, key, account=user_id, now=self.clock())
        self.accounts.audit(user_id, "conversion_executed", quote_id=quote_id)
        return result

    # ---- payout accounts ------------------------------------------------------------

    def resolve_account(self, user_id: str, account_number: Any) -> dict[str, Any]:
        candidates = self.payouts.resolve(account_number)
        return {"account_number": account_number, "candidates": candidates, "simulated": True,
                "note": "The check digit narrows the account to these banks; the account name confirms which one."}

    def add_payout_account(self, user_id: str, currency: Any, label: Any, destination: Any, pin: Any = None,
                           ip: str = "") -> dict[str, Any]:
        if not isinstance(destination, dict):
            raise InvalidOperation("destination must be an object")
        self._authorize(user_id, pin, ip)                       # changes where money can be sent, so it needs the PIN
        result = self.payouts.add(user_id, self._currency(currency), label, destination)
        self.accounts.audit(user_id, "payout_account_added", beneficiary_id=result["beneficiary_id"])
        return result

    def payout_accounts(self, user_id: str) -> list[dict[str, Any]]:
        return self.payouts.list(user_id)

    def delete_payout_account(self, user_id: str, beneficiary_id: Any, pin: Any = None, ip: str = "") -> None:
        self._authorize(user_id, pin, ip)
        self.payouts.delete(user_id, beneficiary_id)
        self.accounts.audit(user_id, "payout_account_deleted", beneficiary_id=beneficiary_id)

    # ---- security centre ----------------------------------------------------------------

    def security(self, user_id: str) -> dict[str, Any]:
        return {**self.accounts.security_status(user_id), "kyc": self.kyc.status(user_id)}

    def kyc_basic(self, user_id: str, full_name: Any, bvn: Any) -> dict[str, Any]:
        result = self.kyc.submit_basic(user_id, full_name, bvn, auto_approve=self.settings.simulation_mode)
        self.accounts.audit(user_id, "kyc_basic_submitted", status=result["status"])
        return result

    def kyc_enhanced(self, user_id: str, address: Any) -> dict[str, Any]:
        result = self.kyc.submit_enhanced(user_id, address, auto_approve=self.settings.simulation_mode)
        self.accounts.audit(user_id, "kyc_enhanced_submitted", status=result["status"])
        return result

    # ---- pools ------------------------------------------------------------------

    def _snapshots(self) -> list[PoolSnapshot]:
        now = self.clock()
        with self._refresh_lock:
            if now - self._last_refresh >= self.settings.pool_refresh_seconds or not self._last_refresh:
                try:
                    for snapshot in self.provider.fetch_pools():
                        self.store.save(snapshot)
                    self.data_status = {"ok": True, "error": "", "as_of": now}
                    self._last_refresh = now
                except ProviderError as error:        # keep serving stored data; it goes stale and is excluded by age
                    log.warning("pool data refresh failed: %s", error)
                    self.data_status = {"ok": False, "error": str(error)[:200], "as_of": self.data_status.get("as_of")}
                    self._last_refresh = now - self.settings.pool_refresh_seconds + 60       # retry in a minute
        snapshots = self.store.latest_all()
        if not snapshots:
            raise ProviderError("no pool data is available yet")
        return snapshots

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
        """Build an auditable DEX screening report without moving funds or changing positions.

        Pool snapshots may be refreshed and persisted by _selector().
        """
        amount = self._amount(amount_minor)
        selector, by_id = self._selector()

        try:
            result = selector.select(amount, now=self.clock())
        except PoolSelectionError as error:
            raise InvalidOperation(str(error)) from None

        evaluations = {evaluation.pool_id: evaluation for evaluation in result.evaluations}
        allocation = result.allocation

        items = []
        for pool_id, amount_minor in sorted(
            allocation.allocations.items(),
            key=lambda item: -item[1],
        ):
            evaluation = evaluations[pool_id]
            snapshot = by_id[pool_id]
            details = allocation.details[pool_id]

            items.append({
                "pool_id": pool_id,
                "amount_minor": amount_minor,
                "tokens": list(evaluation.tokens),
                "chain": evaluation.chain,
                "category": evaluation.category.value,
                "net_apr": evaluation.net_apr,
                "fee_apr": evaluation.fee_apr,
                "il_apr": evaluation.il_apr,
                "depeg_haircut_apr": evaluation.depeg_haircut_apr,
                "risk_score": evaluation.risk_score,
                "net_apr_after_gas": details["net_apr_after_gas"],
                "tvl_usd": evaluation.tvl_usd,
                "daily_volume_usd": evaluation.daily_volume_usd,
                "share_of_pool_tvl": details["share_of_pool_tvl"],
                "snapshot_as_of": snapshot.as_of,
                "source": snapshot.source,
                "dex": snapshot.dex,
                "warnings": list(evaluation.warnings),
            })

        pool_evaluations = []
        for evaluation in result.evaluations:
            snapshot = by_id[evaluation.pool_id]
            row = evaluation.to_dict()
            row.update({
                "snapshot_as_of": snapshot.as_of,
                "source": snapshot.source,
                "dex": snapshot.dex,
                "fee_tier_bps": snapshot.fee_tier_bps,
                "latest_price": snapshot.latest_price,
                "reported_fee_apr": snapshot.reported_fee_apr,
                "selected_amount_minor": allocation.allocations.get(
                    evaluation.pool_id, 0
                ),
                "excluded_reasons": list(
                    result.excluded.get(evaluation.pool_id, [])
                ),
            })
            pool_evaluations.append(row)

        cfg = selector.config
        allocated = allocation.allocated_minor

        return {
            # Existing response fields retained for API compatibility.
            "allocations": items,
            "reserve_minor": allocation.reserve_minor,
            "unallocated_minor": allocation.unallocated_minor,
            "excluded": result.excluded,
            "rationale": allocation.rationale,
            "disclaimer": (
                "Research screening only. Estimates are not guaranteed returns "
                "or investment advice. No funds are moved and no positions "
                "are opened or marked by this report."
            ),

            # Additional audit fields.
            "report_type": "dex_pool_screening",
            "total_capital_minor": allocation.total_capital_minor,
            "allocated_minor": allocated,
            "pool_evaluations": pool_evaluations,
            "data_status": dict(self.data_status),
            "assumptions": {
                "currency": "USDT",
                "amount_unit": "micro-USDT (1 USDT = 1,000,000 units)",
                "horizon_days": cfg.horizon_days,
                "reserve_fraction": cfg.reserve_fraction,
                "max_snapshot_age_hours": cfg.max_snapshot_age_hours,
                "min_tvl_usd": cfg.min_tvl_usd,
                "min_daily_volume_usd": cfg.min_daily_volume_usd,
                "min_history_points": cfg.min_history_points,
                "max_pool_weight": cfg.max_pool_weight,
                "max_pool_tvl_share": cfg.max_pool_tvl_share,
                "model_scope": (
                    "Full-range constant-product-style exposure; "
                    "concentrated-liquidity range management is not modelled."
                ),
                "data_limitations": (
                    "Results depend on snapshot quality and model assumptions. "
                    "Estimated fee APR and impermanent loss are not forecasts."
                ),
            },
            "execution": {
                "performed": False,
                "funds_moved": False,
                "positions_changed": False,
                "note": (
                    "This endpoint screens and allocates hypothetically. "
                    "Separate deployment operations are required to open "
                    "paper positions."
                ),
            },
        }

    def scenario(self, pool_id: Any, price_change_pct: float, days: int = 30, amount_usdt: float = 1000.0) -> dict[str, Any]:
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

    def _pool_cap(self, snapshot: PoolSnapshot) -> int:
        return int(self.selector_config.max_pool_tvl_share * snapshot.tvl_usd * MICRO)

    def _pool_capacity(self, snapshot: PoolSnapshot, amount: int) -> None:
        cap = self._pool_cap(snapshot)
        used = self.positions.pool_exposure(snapshot.pool_id)
        if used + amount > cap:
            raise LimitExceeded(f"pool capacity reached: at most {cap} micro-USDT in total ({used} already placed)")

    def open_position(self, user_id: str, pool_id: Any, amount_minor: Any, client_key: Any, pin: Any = None,
                      ip: str = "") -> dict[str, Any]:
        amount, key = self._amount(amount_minor), self._key(user_id, "pos", client_key)
        self._authorize(user_id, pin, ip, key)
        selector, by_id = self._selector()
        snapshot = by_id.get(pool_id) if isinstance(pool_id, str) else None
        if snapshot is None:
            raise NotFound("pool not found")
        evaluation = selector.evaluate(snapshot, self.clock())
        existing = self._operation_exists(key)
        if not existing:
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
        result = self.positions.open_position(
            user_id,
            snapshot.pool_id,
            snapshot.token0,
            snapshot.token1,
            amount,
            key,
            entry_price=evaluation.latest_price,
            entry_prices=(evaluation.price0_usd, evaluation.price1_usd),
            pool_capacity_minor=self._pool_cap(snapshot),
        )
        if not existing and self._mode.get() == "live":
            try:
                if self.chain is None:
                    raise ProviderError("no chain executor is configured")
                self.positions.set_external_ref(result["position_id"], self.chain.open_position(snapshot, amount, result["position_id"]))
            except Exception as error:      # noqa: BLE001 - the funds were debited; undo that if the chain refused
                self._rollback_position(user_id, result["position_id"], key)
                raise ProviderError("the position could not be opened on-chain; your funds were returned") from error
        if not existing:
            self.accounts.audit(user_id, "position_opened", pool_id=snapshot.pool_id, amount_minor=amount,
                                position_id=result["position_id"])
        return result

    def _rollback_position(self, user_id: str, position_id: str, key: str) -> None:
        position = self.positions.get_position(position_id, account=user_id)
        self.positions.mark_to_market(position_id, position["principal_minor"], 0, source="rollback", as_of=self.clock())
        self.positions.close_position(position_id, f"rollback:{key}", account=user_id)

    def deploy_recommendation(self, user_id: str, amount_minor: Any, client_key: Any, pin: Any = None,
                              ip: str = "") -> list[dict[str, Any]]:
        amount, key = self._amount(amount_minor), self._key(user_id, "deploy", client_key)
        if self._mode.get() == "live":
            raise LiveUnavailable(self.live_requirements(user_id), "open live positions one at a time")
        if not self._prefix_exists(key + ":"):                  # a replay of a finished deploy needs no PIN
            self._authorize(user_id, pin, ip)
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
        """Paper valuation of an open position from current pool data; the customer never supplies a valuation.

        Value (constant-product, full range, before fees) is the principal scaled by sqrt(r0 * r1), where r0 and r1
        are each token's USD price change since entry. That is exact for a 50/50 deposit and includes both impermanent
        loss and the tokens' own price moves. Without USD prices it falls back to impermanent loss from the pool's
        relative price. Fees accrue from the last mark at the pool's current fee APR. Nothing is executed on-chain."""
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
        evaluation = selector.evaluate(snapshot, now)
        p0, p1 = evaluation.price0_usd, evaluation.price1_usd
        e0, e1 = position.get("entry_price0_usd"), position.get("entry_price1_usd")
        entry, price = position.get("entry_price"), evaluation.latest_price

        if (
            p0 is not None and p1 is not None
            and e0 is not None and e1 is not None
            and e0 > 0 and e1 > 0
        ):
            value = principal * math.sqrt((p0 / e0) * (p1 / e1))
        elif (
            entry is not None and price is not None
            and entry > 0 and price > 0
        ):
            value = principal * math.sqrt(price / entry)
        else:
            # Keep the last defensible market value when fresh prices are
            # unavailable. Fee accrual is independent of market valuation.
            value = float(position["current_value_minor"])

        since = last if last is not None else (position.get("opened_ts") or now)
        elapsed = max(0.0, now - since) * self.settings.time_scale
        accrued = (
            position["accrued_fees_minor"]
            + principal * evaluation.fee_apr * elapsed / (365.0 * 86_400)
        )
        ceiling = principal * self.positions.max_mark_multiple
        fees = max(int(min(accrued, max(ceiling - value, 0.0))), position["accrued_fees_minor"])
        value = int(min(value, ceiling - fees))
        self.positions.mark_to_market(position["position_id"], max(value, 0), fees, source="paper-model",
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
                                                * self.settings.time_scale / 86_400, 2)})
        return out

    def close_position(self, user_id: str, position_id: Any, client_key: Any, pin: Any = None, ip: str = "") -> dict[str, Any]:
        key = self._key(user_id, "close", client_key)
        self._authorize(user_id, pin, ip, key)
        position = self.positions.get_position(position_id if isinstance(position_id, str) else "", account=user_id)
        if position["status"] != "closed":
            selector, by_id = self._selector()
            self._mark(position, by_id, selector, force=True)
        if self._mode.get() == "live" and position["status"] != "closed" and position.get("external_ref"):
            if self.chain is None:
                raise ProviderError("no chain executor is configured")
            self.chain.close_position(position["external_ref"])          # if this raises, the position stays open
        result = self.positions.close_position(position["position_id"], key, account=user_id)
        self.accounts.audit(user_id, "position_closed", position_id=position["position_id"],
                            returned_minor=result["returned_usdt_micro"])
        return result

    def activity(self, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return self.accounts.recent_audit(user_id, limit)
