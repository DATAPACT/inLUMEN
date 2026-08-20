from __future__ import annotations

import json
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


class CodegenRunStore:
    """Durable inLUMEN-side metadata for service-executed codegen jobs."""

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
                CREATE TABLE IF NOT EXISTS codegen_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    record_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS codegen_runs_updated_idx
                ON codegen_runs(updated_at DESC)
                """
            )
            self._connection.commit()

    def put(self, record: dict[str, Any]) -> None:
        run_id = str(record.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("A codegen run record requires run_id.")
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO codegen_runs (
                    run_id, status, created_at, updated_at, record_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    record_json = excluded.record_json
                """,
                (
                    run_id,
                    str(record.get("status") or "queued"),
                    str(record.get("created_at") or ""),
                    str(record.get("updated_at") or ""),
                    encoded,
                ),
            )
            self._connection.commit()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT record_json FROM codegen_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["record_json"]))
        return deepcopy(payload) if isinstance(payload, dict) else None

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT record_json
                FROM codegen_runs
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (min(max(int(limit), 1), 100),),
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["record_json"]))
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def clear(self) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM codegen_runs")
            self._connection.commit()
