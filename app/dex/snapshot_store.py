"""Append-only SQLite store for pool snapshots, so every score can be traced to the data it used."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from .pool_data import PoolSnapshot


class SnapshotStore:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock, keep_per_pool: int = 200) -> None:
        self.db, self.lock, self.keep_per_pool = db, lock, keep_per_pool
        with lock:
            db.execute("""
                CREATE TABLE IF NOT EXISTS pool_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pool_id TEXT NOT NULL,
                    as_of REAL NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(pool_id, as_of, source)
                )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_pool ON pool_snapshots(pool_id, snapshot_id DESC)")

    def save(self, snapshot: PoolSnapshot) -> bool:
        payload = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.lock:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO pool_snapshots(pool_id, as_of, source, payload_json) VALUES (?, ?, ?, ?)",
                (snapshot.pool_id, snapshot.as_of, snapshot.source, payload))
            inserted = cursor.rowcount == 1
            if inserted:
                self.db.execute(
                    "DELETE FROM pool_snapshots WHERE pool_id = ? AND snapshot_id NOT IN "
                    "(SELECT snapshot_id FROM pool_snapshots WHERE pool_id = ? ORDER BY snapshot_id DESC LIMIT ?)",
                    (snapshot.pool_id, snapshot.pool_id, self.keep_per_pool))
            return inserted

    def latest(self, pool_id: str) -> PoolSnapshot | None:
        with self.lock:
            row = self.db.execute("SELECT payload_json FROM pool_snapshots WHERE pool_id = ? "
                                  "ORDER BY snapshot_id DESC LIMIT 1", (pool_id,)).fetchone()
        return PoolSnapshot.from_dict(json.loads(row[0])) if row else None

    def latest_all(self) -> list[PoolSnapshot]:
        with self.lock:
            rows = self.db.execute(
                "SELECT payload_json FROM pool_snapshots WHERE snapshot_id IN "
                "(SELECT MAX(snapshot_id) FROM pool_snapshots GROUP BY pool_id) ORDER BY pool_id").fetchall()
        return [PoolSnapshot.from_dict(json.loads(row[0])) for row in rows]

    def history(self, pool_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        with self.lock:
            rows = self.db.execute("SELECT snapshot_id, as_of, source FROM pool_snapshots WHERE pool_id = ? "
                                   "ORDER BY snapshot_id DESC LIMIT ?", (pool_id, limit)).fetchall()
        return [dict(row) for row in rows]
