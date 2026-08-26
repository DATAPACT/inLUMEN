from __future__ import annotations

import json
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


class PipelineRunStore:
    """Durable storage for run records and their immutable private snapshots."""

    def __init__(self, database_path: str):
        self.database_path = database_path
        self._lock = threading.RLock()
        if database_path != ":memory:":
            path = Path(database_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            database_path = str(path)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    snapshot_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS pipeline_runs_updated_idx
                ON pipeline_runs(updated_at DESC)
                """
            )
            self._connection.commit()

    def save(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO pipeline_runs (
                    run_id, idempotency_key, snapshot_sha256, status,
                    created_at, updated_at, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    idempotency_key = excluded.idempotency_key,
                    snapshot_sha256 = excluded.snapshot_sha256,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    record_json = excluded.record_json
                """,
                (
                    record["run_id"],
                    record.get("_idempotency_key"),
                    record["snapshot"]["graph_sha256"],
                    record["status"],
                    record["created_at"],
                    record["updated_at"],
                    encoded,
                ),
            )
            self._connection.commit()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT record_json FROM pipeline_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._decode(row)

    def get_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT record_json FROM pipeline_runs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return self._decode(row)

    def list(self, *, limit: int | None = 20) -> list[dict[str, Any]]:
        parameters: tuple[int, ...] = ()
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters = (min(max(int(limit), 1), 100),)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT record_json FROM pipeline_runs
                ORDER BY updated_at DESC, created_at DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [record for row in rows if (record := self._decode(row)) is not None]

    def clear(self) -> int:
        """Delete every persisted run record and return the removed count."""
        with self._lock:
            row = self._connection.execute(
                "SELECT count(*) AS run_count FROM pipeline_runs"
            ).fetchone()
            count = int(row["run_count"] if row is not None else 0)
            self._connection.execute("DELETE FROM pipeline_runs")
            self._connection.commit()
        return count

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = json.loads(str(row["record_json"]))
        return deepcopy(payload) if isinstance(payload, dict) else None
