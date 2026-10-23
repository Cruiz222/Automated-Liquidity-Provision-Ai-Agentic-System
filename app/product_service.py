"""Local simulated wallet and DEX LP-position services; not production custody."""
from __future__ import annotations
import json, sqlite3, threading, uuid
from pathlib import Path
from typing import Any
from .wallet_ledger import WalletLedger, LedgerError, InvalidOperation

class ProductServiceError(Exception): pass
class NotFound(ProductServiceError): pass
class Conflict(ProductServiceError): pass

class ProductService:
    def __init__(self, database: str | Path = ":memory:", ledger: WalletLedger | None = None):
        self.database=str(database); self._lock=threading.RLock()
        self.db=sqlite3.connect(self.database, check_same_thread=False, isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS lp_positions(
          position_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, pool_id TEXT NOT NULL,
          chain TEXT NOT NULL, dex TEXT NOT NULL, token0 TEXT NOT NULL, token1 TEXT NOT NULL,
          deposited_usd TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
          metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          closed_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_lp_user ON lp_positions(user_id, created_at);
        """)
        self.ledger=ledger or WalletLedger(self.database)
        self._owns_ledger=ledger is None
    def close(self):
        self.db.close()
        if self._owns_ledger: self.ledger.close()
    def balances(self, user_id: str) -> dict[str,int]:
        self._require_user(user_id); return self.ledger.balances(user_id)
    def transactions(self, user_id: str, limit: int=50) -> list[dict[str,Any]]:
        self._require_user(user_id)
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= 200: raise InvalidOperation('limit must be 1..200')
        rows=self.ledger.entries(user_id)[-limit:]
        return rows
    def deposit_simulated(self,user_id:str,currency:str,amount_minor:int,idempotency_key:str):
        self._require_user(user_id); return self.ledger.deposit(user_id,currency,amount_minor,idempotency_key)
    def withdraw_simulated(self,user_id:str,currency:str,amount_minor:int,idempotency_key:str):
        self._require_user(user_id); return self.ledger.withdraw(user_id,currency,amount_minor,idempotency_key)
    def create_position(self,user_id:str,data:dict[str,Any])->dict[str,Any]:
        self._require_user(user_id)
        required=('pool_id','chain','dex','token0','token1','deposited_usd')
        if not isinstance(data,dict) or any(not isinstance(data.get(k),str) or not data[k].strip() for k in required):
            raise InvalidOperation('required position fields must be non-empty strings')
        from decimal import Decimal, InvalidOperation as DecInvalid
        try: amount=Decimal(data['deposited_usd'])
        except (DecInvalid,ValueError): raise InvalidOperation('deposited_usd must be decimal') from None
        if not amount.is_finite() or amount <= 0: raise InvalidOperation('deposited_usd must be positive and finite')
        if data['token0'].upper()==data['token1'].upper(): raise InvalidOperation('position tokens must differ')
        metadata=data.get('metadata',{})
        if not isinstance(metadata,dict): raise InvalidOperation('metadata must be an object')
        pid=str(uuid.uuid4())
        with self._lock:
            self.db.execute('INSERT INTO lp_positions(position_id,user_id,pool_id,chain,dex,token0,token1,deposited_usd,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
              (pid,user_id,*(data[k].strip() for k in ('pool_id','chain','dex','token0','token1')),str(amount), 'open',json.dumps(metadata,sort_keys=True)))
        return self.get_position(user_id,pid)
    def list_positions(self,user_id:str,status:str|None=None)->list[dict[str,Any]]:
        self._require_user(user_id)
        if status not in (None,'open','closed'): raise InvalidOperation('status must be open or closed')
        sql='SELECT * FROM lp_positions WHERE user_id=?'; args=[user_id]
        if status: sql+=' AND status=?'; args.append(status)
        sql+=' ORDER BY created_at DESC, position_id'
        return [self._position(r) for r in self.db.execute(sql,args).fetchall()]
    def get_position(self,user_id:str,position_id:str)->dict[str,Any]:
        self._require_user(user_id)
        row=self.db.execute('SELECT * FROM lp_positions WHERE position_id=? AND user_id=?',(position_id,user_id)).fetchone()
        if row is None: raise NotFound('position not found')
        return self._position(row)
    def close_position(self,user_id:str,position_id:str)->dict[str,Any]:
        with self._lock:
            current=self.get_position(user_id,position_id)
            if current['status']=='closed': return current
            self.db.execute("UPDATE lp_positions SET status='closed',closed_at=CURRENT_TIMESTAMP WHERE position_id=? AND user_id=?",(position_id,user_id))
        return self.get_position(user_id,position_id)
    @staticmethod
    def _position(row):
        d=dict(row); d['metadata']=json.loads(d.pop('metadata_json')); return d
    @staticmethod
    def _require_user(user_id):
        if not isinstance(user_id,str) or not user_id.strip(): raise InvalidOperation('user_id required')
