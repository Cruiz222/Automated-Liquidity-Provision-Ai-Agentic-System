"""Runtime settings. Defaults are safe for local simulation; nothing here enables real-money behaviour."""
from __future__ import annotations

import os
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
    secure_cookies: bool = False            # set True only when served over HTTPS
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
    sim_time_scale: float = 1440.0          # simulated seconds of fee accrual per real second (1 min ~ 1 day)

    def __post_init__(self) -> None:
        if self.host not in LOOPBACK_HOSTS:
            raise ValueError("the development server may only bind to a loopback address")
        if not 0 <= self.port <= 65535:
            raise ValueError("port out of range")
        if self.password_iterations < 1 or self.sim_time_scale < 0:
            raise ValueError("invalid settings")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        defaults = cls()
        return cls(
            host=env.get("DEX_HOST", defaults.host), port=int(env.get("DEX_PORT", defaults.port)),
            database=env.get("DEX_DB", defaults.database),
            pool_snapshot_path=env.get("DEX_POOLS", defaults.pool_snapshot_path),
            secure_cookies=env.get("DEX_SECURE_COOKIES", "") == "1",
            sim_time_scale=float(env.get("DEX_SIM_TIME_SCALE", defaults.sim_time_scale)),
        )
