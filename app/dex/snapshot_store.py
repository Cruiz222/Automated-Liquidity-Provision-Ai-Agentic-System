"""SQLite persistence for DEX pool snapshots."""

import json
import math
import sqlite3
import threading

from .pool_data import PoolSnapshot


class SnapshotStore:
    def __init__(
        self,
        db: sqlite3.Connection,
        lock: threading.RLock,
        keep_per_pool: int | None = 200,
    ):
        if keep_per_pool is not None:
            if (
                isinstance(keep_per_pool, bool)
                or not isinstance(keep_per_pool, int)
                or keep_per_pool < 1
            ):
                raise ValueError(
                    "keep_per_pool must be a positive integer or None"
                )

        self.db = db
        self.lock = lock
        self.keep_per_pool = keep_per_pool

        with self.lock:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS pool_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pool_id TEXT NOT NULL,
                    as_of REAL NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(pool_id, as_of, source)
                )
                """
            )
            self.db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshots_pool
                ON pool_snapshots(pool_id, snapshot_id DESC)
                """
            )
            self.db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshots_pool_time
                ON pool_snapshots(pool_id, as_of, snapshot_id)
                """
            )

    @staticmethod
    def _serialize(snapshot: PoolSnapshot) -> str:
        return json.dumps(
            snapshot.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def save(self, snapshot: PoolSnapshot) -> bool:
        """Save a snapshot atomically with its retention cleanup.

        Returns True when inserted and False for an identical duplicate.
        Raises ValueError if the same unique key has different content.

        A transaction started here is committed or rolled back here.
        An existing caller-owned transaction is preserved through a savepoint.
        """
        payload = self._serialize(snapshot)

        with self.lock:
            owns_transaction = not self.db.in_transaction

            if owns_transaction:
                self.db.execute("BEGIN")

            savepoint_created = False

            try:
                self.db.execute("SAVEPOINT save_snapshot")
                savepoint_created = True

                cursor = self.db.execute(
                    """
                    INSERT OR IGNORE INTO pool_snapshots
                        (pool_id, as_of, source, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        snapshot.pool_id,
                        snapshot.as_of,
                        snapshot.source,
                        payload,
                    ),
                )

                inserted = cursor.rowcount == 1

                if not inserted:
                    existing = self.db.execute(
                        """
                        SELECT payload_json
                        FROM pool_snapshots
                        WHERE pool_id = ? AND as_of = ? AND source = ?
                        """,
                        (
                            snapshot.pool_id,
                            snapshot.as_of,
                            snapshot.source,
                        ),
                    ).fetchone()

                    if existing is None:
                        raise RuntimeError(
                            "Snapshot insert was ignored, but the existing "
                            "record could not be found"
                        )

                    if existing[0] != payload:
                        raise ValueError(
                            "Conflicting snapshot: pool_id, as_of and source "
                            "already exist with different content"
                        )

                if inserted and self.keep_per_pool is not None:
                    self.db.execute(
                        """
                        DELETE FROM pool_snapshots
                        WHERE pool_id = ?
                          AND snapshot_id NOT IN (
                              SELECT snapshot_id
                              FROM pool_snapshots
                              WHERE pool_id = ?
                              ORDER BY snapshot_id DESC
                              LIMIT ?
                          )
                        """,
                        (
                            snapshot.pool_id,
                            snapshot.pool_id,
                            self.keep_per_pool,
                        ),
                    )

                self.db.execute("RELEASE SAVEPOINT save_snapshot")
                savepoint_created = False

                if owns_transaction:
                    self.db.execute("COMMIT")

                return inserted

            except BaseException:
                if savepoint_created:
                    self.db.execute("ROLLBACK TO SAVEPOINT save_snapshot")
                    self.db.execute("RELEASE SAVEPOINT save_snapshot")

                if owns_transaction and self.db.in_transaction:
                    self.db.execute("ROLLBACK")

                raise

    def latest(self, pool_id: str) -> PoolSnapshot | None:
        with self.lock:
            row = self.db.execute(
                """
                SELECT payload_json
                FROM pool_snapshots
                WHERE pool_id = ?
                ORDER BY snapshot_id DESC
                LIMIT 1
                """,
                (pool_id,),
            ).fetchone()

        if row is None:
            return None

        return PoolSnapshot.from_dict(json.loads(row[0]))

    def latest_all(self) -> list[PoolSnapshot]:
        with self.lock:
            rows = self.db.execute(
                """
                SELECT payload_json
                FROM pool_snapshots
                WHERE snapshot_id IN (
                    SELECT MAX(snapshot_id)
                    FROM pool_snapshots
                    GROUP BY pool_id
                )
                ORDER BY pool_id
                """
            ).fetchall()

        return [
            PoolSnapshot.from_dict(json.loads(row[0]))
            for row in rows
        ]

    def latest_all_as_of(
        self,
        cutoff_timestamp: float,
    ) -> list[PoolSnapshot]:
        """Return each pool's latest snapshot recorded at or before cutoff.

        If multiple sources recorded a snapshot at the same timestamp,
        the most recently inserted record wins.

        Pools without an eligible snapshot are excluded.
        Results are ordered by pool_id.
        """
        if (
            isinstance(cutoff_timestamp, bool)
            or not isinstance(cutoff_timestamp, (int, float))
            or not math.isfinite(cutoff_timestamp)
            or cutoff_timestamp < 0
        ):
            raise ValueError(
                "cutoff_timestamp must be a non-negative finite timestamp"
            )

        with self.lock:
            rows = self.db.execute(
                """
                SELECT snapshot.payload_json
                FROM pool_snapshots AS snapshot
                WHERE snapshot.snapshot_id = (
                    SELECT candidate.snapshot_id
                    FROM pool_snapshots AS candidate
                    WHERE candidate.pool_id = snapshot.pool_id
                      AND candidate.as_of <= ?
                    ORDER BY candidate.as_of DESC,
                             candidate.snapshot_id DESC
                    LIMIT 1
                )
                ORDER BY snapshot.pool_id
                """,
                (cutoff_timestamp,),
            ).fetchall()

        return [
            PoolSnapshot.from_dict(json.loads(row[0]))
            for row in rows
        ]

    def history(
        self,
        pool_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Return snapshot metadata, newest first."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
        ):
            raise ValueError("limit must be a positive integer")

        with self.lock:
            rows = self.db.execute(
                """
                SELECT snapshot_id, as_of, source
                FROM pool_snapshots
                WHERE pool_id = ?
                ORDER BY snapshot_id DESC
                LIMIT ?
                """,
                (pool_id, limit),
            ).fetchall()

        return [
            {
                "snapshot_id": row[0],
                "as_of": row[1],
                "source": row[2],
            }
            for row in rows
        ]

    def history_snapshots(
        self,
        pool_id: str,
        start_as_of: float | None = None,
        end_as_of: float | None = None,
        limit: int | None = None,
    ) -> list[PoolSnapshot]:
        """Return complete snapshots, oldest first.

        Time bounds are inclusive Unix timestamps.
        The optional limit returns the earliest matching records.
        """
        for name, value in (
            ("start_as_of", start_as_of),
            ("end_as_of", end_as_of),
        ):
            if value is not None:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError(
                        f"{name} must be a finite timestamp"
                    )

        if (
            start_as_of is not None
            and end_as_of is not None
            and start_as_of > end_as_of
        ):
            raise ValueError(
                "start_as_of cannot be later than end_as_of"
            )

        if limit is not None:
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
            ):
                raise ValueError(
                    "limit must be a positive integer or None"
                )

        conditions = ["pool_id = ?"]
        params: list[object] = [pool_id]

        if start_as_of is not None:
            conditions.append("as_of >= ?")
            params.append(start_as_of)

        if end_as_of is not None:
            conditions.append("as_of <= ?")
            params.append(end_as_of)

        query = (
            "SELECT payload_json FROM pool_snapshots WHERE "
            + " AND ".join(conditions)
            + " ORDER BY as_of ASC, snapshot_id ASC"
        )

        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with self.lock:
            rows = self.db.execute(query, params).fetchall()

        return [
            PoolSnapshot.from_dict(json.loads(row[0]))
            for row in rows
        ]
