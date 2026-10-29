"""Know-your-customer tiers that drive withdrawal limits.

Tier 0 unverified, tier 1 identity (name + BVN), tier 2 enhanced (address). In simulation mode submissions are
auto-approved after format checks; otherwise they stay 'pending' until a real verification provider is wired in
(this module deliberately cannot approve anything by itself outside simulation).

Data minimisation: the BVN is never stored. Only its last four digits and a keyed hash (to stop one BVN being
used on several accounts) are kept. Handle any real identity data under Nigeria's data-protection rules.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Any, Callable

from ..ledger import CurrencyLimits, WalletLedger

# Withdrawal limits per tier (kobo / micro-USDT). Placeholders to review with compliance advice.
TIER_LIMITS: dict[int, dict[str, CurrencyLimits]] = {
    0: {"NGN": CurrencyLimits(10_000, 5_000_000, 5_000_000, 5_000),
        "USDT": CurrencyLimits(5_000_000, 50_000_000, 50_000_000, 1_000_000)},
    1: {"NGN": CurrencyLimits(10_000, 100_000_000, 300_000_000, 5_000),
        "USDT": CurrencyLimits(5_000_000, 1_000_000_000, 3_000_000_000, 1_000_000)},
    2: {"NGN": CurrencyLimits(10_000, 500_000_000, 1_000_000_000, 5_000),
        "USDT": CurrencyLimits(5_000_000, 5_000_000_000, 10_000_000_000, 1_000_000)},
}


class KycError(ValueError):
    pass


class KycConflict(KycError):
    pass


class KycStore:
    def __init__(self, ledger: WalletLedger, clock: Callable[[], float] = time.time) -> None:
        self.ledger, self.db, self.clock = ledger, ledger.db, clock
        with ledger.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS kyc (
                user_id TEXT PRIMARY KEY, tier INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'none',
                full_name TEXT NOT NULL DEFAULT '', bvn_last4 TEXT NOT NULL DEFAULT '', bvn_hash TEXT UNIQUE,
                address TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0)""")
            self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('kyc_pepper', ?)", (secrets.token_hex(32),))

    def _pepper(self) -> bytes:
        with self.ledger.lock:
            return bytes.fromhex(self.db.execute("SELECT value FROM meta WHERE key = 'kyc_pepper'").fetchone()[0])

    def status(self, user_id: str) -> dict[str, Any]:
        with self.ledger.lock:
            row = self.db.execute("SELECT tier, status, bvn_last4, updated_at FROM kyc WHERE user_id = ?", (user_id,)).fetchone()
        tier = row["tier"] if row else 0
        return {"tier": tier, "status": row["status"] if row else "none", "bvn_last4": row["bvn_last4"] if row else "",
                "limits": {c: vars(l) for c, l in TIER_LIMITS[tier].items()}}

    def tier(self, user_id: str) -> int:
        return self.status(user_id)["tier"]

    def limits(self, user_id: str, currency: str) -> CurrencyLimits:
        return TIER_LIMITS[self.tier(user_id)][currency]

    def submit_basic(self, user_id: str, full_name: Any, bvn: Any, auto_approve: bool) -> dict[str, Any]:
        if not isinstance(full_name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{2,79}", full_name.strip()):
            raise KycError("enter your full name as on your ID (letters only)")
        if not isinstance(bvn, str) or not re.fullmatch(r"\d{11}", bvn) or len(set(bvn)) == 1:
            raise KycError("BVN must be 11 digits")
        digest = hmac.new(self._pepper(), bvn.encode(), hashlib.sha256).hexdigest()
        with self.ledger.transaction():
            current = self.db.execute("SELECT tier, status FROM kyc WHERE user_id = ?", (user_id,)).fetchone()
            if current and current["tier"] >= 1:
                raise KycError("identity already verified")
            if self.db.execute("SELECT 1 FROM kyc WHERE bvn_hash = ? AND user_id != ?", (digest, user_id)).fetchone():
                raise KycConflict("this BVN is already linked to another account")
            tier, status = (1, "approved") if auto_approve else (0, "pending")
            self.db.execute(
                "INSERT INTO kyc(user_id, tier, status, full_name, bvn_last4, bvn_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET tier=excluded.tier, status=excluded.status, full_name=excluded.full_name, "
                "bvn_last4=excluded.bvn_last4, bvn_hash=excluded.bvn_hash, updated_at=excluded.updated_at",
                (user_id, tier, status, full_name.strip(), bvn[-4:], digest, self.clock()))
        return self.status(user_id)

    def submit_enhanced(self, user_id: str, address: Any, auto_approve: bool) -> dict[str, Any]:
        if not isinstance(address, str) or not 10 <= len(address.strip()) <= 200:
            raise KycError("enter your residential address (10 to 200 characters)")
        with self.ledger.transaction():
            row = self.db.execute("SELECT tier FROM kyc WHERE user_id = ?", (user_id,)).fetchone()
            if not row or row["tier"] < 1:
                raise KycError("complete identity verification (tier 1) first")
            if row["tier"] >= 2:
                raise KycError("already at the highest tier")
            tier, status = (2, "approved") if auto_approve else (1, "pending")
            self.db.execute("UPDATE kyc SET tier = ?, status = ?, address = ?, updated_at = ? WHERE user_id = ?",
                            (tier, status, address.strip(), self.clock(), user_id))
        return self.status(user_id)
