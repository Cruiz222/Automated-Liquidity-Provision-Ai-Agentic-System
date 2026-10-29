"""Runtime settings. Defaults are safe for local simulation; nothing here enables real-money behaviour."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8010
    database: str = "data/platform.sqlite3"
    pool_snapshot_path: str = str(Path(__file__).resolve().parent.parent / "examples" / "pools_snapshot.json")
    simulation_mode: bool = True            # gates the simulated deposit and payout endpoints
    require_pin: bool = True                # every money-moving action needs the customer's transaction PIN (tests only)
    secret_key_path: str = ""               # server-side secret (PIN pepper); default: next to the database
    secure_cookies: bool = False            # set True only when served over HTTPS
    public_host: str = ""                   # e.g. "lab.example.com" when a TLS proxy fronts the server
    public_https: bool = False              # the public site is HTTPS (adds Secure cookies, HSTS, https origin)
    trust_proxy: bool = False               # use X-Forwarded-For from a proxy on this machine for client addresses
    smtp_host: str = ""                     # set to send real email over STARTTLS; empty = console/outbox file only
    smtp_port: int = 587
    smtp_from: str = ""
    smtp_user: str = ""
    smtp_password: str = ""
    live_enabled: bool = False              # create the separate live books at all (off by default)
    live_rails: str = ""                    # "sandbox" is the only implemented value; empty = no rails
    live_paused: bool = False               # operator kill switch: blocks every live action
    live_database: str = ""                 # live ledger file; default: live_ledger.sqlite3 beside the main database
    webhook_secret: str = ""                # shared secret for signed webhooks (32+ characters)
    banks_path: str = ""                    # optional replacement bank directory (JSON); default ships a starter list
    # accounts and sessions
    password_iterations: int = 600_000
    session_ttl_seconds: int = 8 * 3600
    session_idle_seconds: int = 3600
    max_sessions_per_user: int = 5
    login_max_failures: int = 5
    login_lock_seconds: int = 900
    # simulated FX (a fixed placeholder rate; not a market rate)
    fx_ngn_per_usdt: str = "1500"
    fx_spread_bps: int = 100
    fx_fee_bps: int = 50
    quote_ttl_seconds: int = 30
    # simulated deposit caps (kobo / micro-USDT)
    sim_deposit_max: tuple[tuple[str, int], ...] = (("NGN", 500_000_000), ("USDT", 5_000_000_000))
    sim_deposit_daily: tuple[tuple[str, int], ...] = (("NGN", 2_000_000_000), ("USDT", 20_000_000_000))
    # positions and pools
    max_open_positions: int = 20
    pool_refresh_seconds: int = 30
    min_mark_interval_seconds: int = 30
    sim_time_scale: float | None = None     # simulated seconds of fee accrual per real second; default 1440 on example data, 1 on live data
    data_source: str = "fixture"            # "fixture" (synthetic example pools) or "live" (DefiLlama pools and prices)
    rate_sources: tuple[str, ...] = ()      # preset names or "url|json.path"; empty = fixed simulation rate
    pool_chains: tuple[str, ...] = ()       # live data only; empty = provider defaults
    pool_projects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.host not in LOOPBACK_HOSTS:
            raise ValueError("the development server may only bind to a loopback address")
        if not 0 <= self.port <= 65535:
            raise ValueError("port out of range")
        if self.password_iterations < 1 or (self.sim_time_scale is not None and self.sim_time_scale < 0):
            raise ValueError("invalid settings")
        if self.data_source not in ("fixture", "live"):
            raise ValueError("data_source must be 'fixture' or 'live'")
        if self.live_enabled:
            if self.live_rails != "sandbox":
                raise ValueError("live mode needs live_rails; only 'sandbox' is implemented, so real-money rails must be added first")
            if len(self.webhook_secret) < 32:
                raise ValueError("webhook_secret must be at least 32 characters when live mode is enabled")
        if self.live_rails not in ("", "sandbox") and self.simulation_mode:
            raise ValueError("real rails require simulation_mode=False so identity checks cannot be auto-approved")
        if self.smtp_host and not self.smtp_from:
            raise ValueError("smtp_from is required when smtp_host is set")
        if self.public_host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}(:\d{1,5})?", self.public_host):
            raise ValueError("public_host must be a plain host name")

    @property
    def time_scale(self) -> float:
        """Real market data runs in real time; the synthetic example runs 1440x faster so returns are visible."""
        if self.sim_time_scale is not None:
            return self.sim_time_scale
        return 1.0 if self.data_source == "live" else 1440.0

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        defaults = cls()
        return cls(
            host=env.get("DEX_HOST", defaults.host), port=int(env.get("DEX_PORT", defaults.port)),
            database=env.get("DEX_DB", defaults.database),
            pool_snapshot_path=env.get("DEX_POOLS", defaults.pool_snapshot_path),
            secure_cookies=env.get("DEX_SECURE_COOKIES", "") == "1",
            public_host=env.get("DEX_PUBLIC_HOST", ""), public_https=env.get("DEX_PUBLIC_HTTPS", "") == "1",
            trust_proxy=env.get("DEX_TRUST_PROXY", "") == "1", banks_path=env.get("DEX_BANKS", ""),
            live_enabled=env.get("DEX_LIVE", "") == "1", live_rails=env.get("DEX_LIVE_RAILS", ""),
            live_paused=env.get("DEX_LIVE_PAUSED", "") == "1", live_database=env.get("DEX_LIVE_DB", ""),
            webhook_secret=env.get("DEX_WEBHOOK_SECRET", ""),
            smtp_host=env.get("DEX_SMTP_HOST", ""), smtp_port=int(env.get("DEX_SMTP_PORT", defaults.smtp_port)),
            smtp_from=env.get("DEX_SMTP_FROM", ""), smtp_user=env.get("DEX_SMTP_USER", ""),
            smtp_password=env.get("DEX_SMTP_PASSWORD", ""),
            sim_time_scale=float(env["DEX_SIM_TIME_SCALE"]) if env.get("DEX_SIM_TIME_SCALE") else None,
            data_source=env.get("DEX_DATA", defaults.data_source),
            rate_sources=tuple(x for x in env.get("DEX_RATE_SOURCES", "").split(",") if x),
            pool_chains=tuple(x for x in env.get("DEX_POOL_CHAINS", "").split(",") if x),
            pool_projects=tuple(x for x in env.get("DEX_POOL_PROJECTS", "").split(",") if x),
        )
