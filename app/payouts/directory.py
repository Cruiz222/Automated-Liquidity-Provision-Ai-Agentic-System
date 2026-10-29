"""Bank directory, account-name enquiry and saved payout accounts (beneficiaries).

Why the bank code is optional for the customer: they enter an account number; the NUBAN check narrows it to the
few banks that could hold it; a name enquiry shows who owns the account at each candidate; the customer confirms
(or picks the bank by name). The code is then stored with the saved account, so it is never typed again.

The simulation name enquiry invents names and is labelled as such. In production it must be replaced by a
payout provider's account-name lookup, which is what makes the answer authoritative.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..ledger import InvalidOperation, WalletLedger
from . import nuban

DEFAULT_BANKS_PATH = Path(__file__).resolve().parent / "nigerian_banks.json"
MAX_BENEFICIARIES = 20
_LABEL = re.compile(r"^[\w .,'&()-]{1,40}$")


class PayoutAccountNotFound(LookupError):
    """No such saved payout account for this user (also used for another user's account)."""


class AmbiguousBank(InvalidOperation):
    """More than one bank could hold the account; the customer must choose. Carries the candidates."""

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        super().__init__("more than one bank could hold this account; choose the bank")
        self.candidates = candidates


@dataclass(frozen=True)
class Bank:
    code: str
    name: str


class NameEnquiry(Protocol):
    def lookup(self, bank_code: str, account_number: str) -> str | None: ...


class BankDirectory:
    def __init__(self, banks: list[Bank]) -> None:
        if len({b.code for b in banks}) != len(banks):
            raise ValueError("duplicate bank codes")
        self.banks = list(banks)
        self._by_code = {b.code: b for b in banks}

    @classmethod
    def load(cls, path: str | Path = DEFAULT_BANKS_PATH) -> "BankDirectory":
        document = json.loads(Path(path).read_text())
        return cls([Bank(str(b["code"]), str(b["name"])) for b in document["banks"]])

    def get(self, code: str) -> Bank | None:
        return self._by_code.get(code)

    def candidates(self, account_number: str) -> list[Bank]:
        """Banks whose NUBAN check digit matches this account number."""
        if not nuban.is_valid_account_number(account_number):
            raise InvalidOperation("account number must be exactly 10 digits")
        return [b for b in self.banks if nuban.validates(b.code, account_number)]


class SimulatedNameEnquiry:
    """Deterministic stand-in: of the candidate banks, exactly one 'owns' the account and returns a made-up name."""

    FIRST = ("Ada", "Chidi", "Funke", "Ibrahim", "Ngozi", "Tunde", "Zainab", "Emeka")
    LAST = ("Obi", "Bello", "Adeyemi", "Okafor", "Musa", "Eze", "Balogun", "Nwosu")

    def __init__(self, directory: BankDirectory) -> None:
        self.directory = directory

    def lookup(self, bank_code: str, account_number: str) -> str | None:
        candidates = [b.code for b in self.directory.candidates(account_number)]
        if not candidates:
            return None
        digest = hashlib.sha256(account_number.encode()).digest()
        if candidates[digest[0] % len(candidates)] != bank_code:
            return None
        return f"SIMULATED: {self.FIRST[digest[1] % 8]} {self.LAST[digest[2] % 8]}"


class PayoutAccounts:
    def __init__(self, ledger: WalletLedger, directory: BankDirectory, enquiry: NameEnquiry) -> None:
        self.ledger, self.db, self.directory, self.enquiry = ledger, ledger.db, directory, enquiry
        self._lock = threading.Lock()
        with ledger.transaction():
            self.db.execute("""CREATE TABLE IF NOT EXISTS beneficiaries (
                beneficiary_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, currency TEXT NOT NULL, label TEXT NOT NULL,
                destination_json TEXT NOT NULL, bank_name TEXT NOT NULL DEFAULT '', account_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, destination_json))""")

    # ---- resolution ------------------------------------------------------------
    def resolve(self, account_number: Any) -> list[dict[str, Any]]:
        """Candidate banks for an account number, each with the account name if the enquiry returned one."""
        if not isinstance(account_number, str):
            raise InvalidOperation("account number must be exactly 10 digits")
        out = []
        for bank in self.directory.candidates(account_number):
            name = self.enquiry.lookup(bank.code, account_number)
            out.append({"bank_code": bank.code, "bank_name": bank.name, "account_name": name})
        out.sort(key=lambda c: (c["account_name"] is None, c["bank_name"]))
        return out

    def complete_ngn_destination(self, destination: dict[str, Any]) -> dict[str, str]:
        """Return {bank_code, account_number, bank_name, account_name}, deriving the bank when it was not given.

        A given bank code is checked against the NUBAN rule when the bank is in the directory (catches typos).
        An omitted code is accepted only when exactly one candidate returns an account name; otherwise the
        customer must choose from the candidates (AmbiguousBank)."""
        number = destination.get("account_number")
        if not isinstance(number, str) or not nuban.is_valid_account_number(number):
            raise InvalidOperation("NGN destination needs a 10-digit account_number")
        code = destination.get("bank_code")
        if code:
            if not isinstance(code, str) or not re.fullmatch(r"\d{3,6}", code):
                raise InvalidOperation("NGN destination needs a numeric bank_code")
            bank = self.directory.get(code)
            if bank is not None and not nuban.validates(code, number):
                raise InvalidOperation("the account number does not match that bank; check both")
            name = self.enquiry.lookup(code, number) if bank is not None else None
            return {"bank_code": code, "account_number": number, "bank_name": bank.name if bank else "",
                    "account_name": name or ""}
        candidates = self.resolve(number)
        named = [c for c in candidates if c["account_name"]]
        if len(named) == 1:
            c = named[0]
            return {"bank_code": c["bank_code"], "account_number": number, "bank_name": c["bank_name"],
                    "account_name": c["account_name"]}
        if not candidates:
            raise InvalidOperation("no known bank matches this account number; check it or enter the bank code")
        raise AmbiguousBank(candidates)

    # ---- saved accounts ---------------------------------------------------------
    @staticmethod
    def _present(row: Any) -> dict[str, Any]:
        d = dict(row)
        d["destination"] = json.loads(d.pop("destination_json"))
        d.pop("user_id", None)
        return d

    def add(self, user_id: str, currency: str, label: Any, destination: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(label, str) or not _LABEL.fullmatch(label.strip()):
            raise InvalidOperation("label must be 1-40 letters, digits or simple punctuation")
        bank_name = account_name = ""
        if currency == "NGN":
            full = self.complete_ngn_destination(destination)
            bank_name, account_name = full.pop("bank_name"), full.pop("account_name")
            clean = full
        else:                                    # USDT: validate with the ledger's own rules
            clean = dict(destination)
            from ..ledger.wallet_ledger import _validate_destination
            clean = _validate_destination("USDT", clean)
        text = json.dumps(clean, sort_keys=True)
        with self._lock, self.ledger.transaction():
            count = self.db.execute("SELECT COUNT(*) FROM beneficiaries WHERE user_id = ?", (user_id,)).fetchone()[0]
            if count >= MAX_BENEFICIARIES:
                raise InvalidOperation(f"at most {MAX_BENEFICIARIES} saved payout accounts")
            if self.db.execute("SELECT 1 FROM beneficiaries WHERE user_id = ? AND destination_json = ?", (user_id, text)).fetchone():
                raise InvalidOperation("this payout account is already saved")
            bid = "b_" + uuid.uuid4().hex
            self.db.execute("INSERT INTO beneficiaries(beneficiary_id, user_id, currency, label, destination_json, bank_name, account_name) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", (bid, user_id, currency, label.strip(), text, bank_name, account_name))
        return self.get(user_id, bid)

    def get(self, user_id: str, beneficiary_id: Any) -> dict[str, Any]:
        with self.ledger.lock:
            row = self.db.execute("SELECT * FROM beneficiaries WHERE beneficiary_id = ? AND user_id = ?",
                                  (beneficiary_id if isinstance(beneficiary_id, str) else "", user_id)).fetchone()
        if row is None:
            raise PayoutAccountNotFound("payout account not found")
        return self._present(row)

    def list(self, user_id: str) -> list[dict[str, Any]]:
        with self.ledger.lock:
            rows = self.db.execute("SELECT * FROM beneficiaries WHERE user_id = ? ORDER BY created_at, beneficiary_id", (user_id,)).fetchall()
        return [self._present(r) for r in rows]

    def delete(self, user_id: str, beneficiary_id: Any) -> None:
        self.get(user_id, beneficiary_id)
        with self.ledger.lock:
            self.db.execute("DELETE FROM beneficiaries WHERE beneficiary_id = ? AND user_id = ?", (beneficiary_id, user_id))
