from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from auth_middleware import current_workspace_id


def _database_url(value: str) -> str:
    raw = str(value or "").strip()
    if raw == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    if "://" in raw:
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    path = Path(raw).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{path}"


class CodegenRunStore:
    """Durable, workspace-scoped metadata for service-executed codegen jobs."""

    def __init__(self, database_path: str):
        self.database_path = database_path
        self._lock = threading.RLock()
        options = (
            {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
            if database_path == ":memory:"
            else {"pool_pre_ping": True}
        )
        self._engine = create_engine(_database_url(database_path), **options)
        with self._engine.begin() as connection:
            connection.execute(
                text("""
                CREATE TABLE IF NOT EXISTS codegen_runs (
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, run_id)
                )
            """)
            )
            connection.execute(
                text("""
                CREATE INDEX IF NOT EXISTS codegen_runs_updated_idx
                ON codegen_runs(workspace_id, updated_at DESC)
            """)
            )

    def put(self, record: dict[str, Any]) -> None:
        run_id = str(record.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("A codegen run record requires run_id.")
        workspace_id = str(record.get("workspace_id") or current_workspace_id())
        record["workspace_id"] = workspace_id
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO codegen_runs (
                    workspace_id, run_id, status, created_at, updated_at, record_json
                ) VALUES (
                    :workspace_id, :run_id, :status, :created_at, :updated_at, :record_json
                )
                ON CONFLICT(workspace_id, run_id) DO UPDATE SET
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    record_json = excluded.record_json
            """),
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "status": str(record.get("status") or "queued"),
                    "created_at": str(record.get("created_at") or ""),
                    "updated_at": str(record.get("updated_at") or ""),
                    "record_json": encoded,
                },
            )

    def get(
        self, run_id: str, workspace_id: str | None = None
    ) -> dict[str, Any] | None:
        resolved = workspace_id or current_workspace_id()
        with self._lock, self._engine.connect() as connection:
            row = connection.execute(
                text("""
                SELECT record_json FROM codegen_runs
                WHERE workspace_id = :workspace_id AND run_id = :run_id
            """),
                {"workspace_id": resolved, "run_id": run_id},
            ).first()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        return deepcopy(payload) if isinstance(payload, dict) else None

    def list(
        self, *, limit: int = 20, workspace_id: str | None = None
    ) -> list[dict[str, Any]]:
        resolved = workspace_id or current_workspace_id()
        with self._lock, self._engine.connect() as connection:
            rows = connection.execute(
                text("""
                SELECT record_json FROM codegen_runs
                WHERE workspace_id = :workspace_id
                ORDER BY updated_at DESC, created_at DESC
                LIMIT :limit
            """),
                {
                    "workspace_id": resolved,
                    "limit": min(max(int(limit), 1), 100),
                },
            ).fetchall()
        records = []
        for row in rows:
            payload = json.loads(str(row[0]))
            if isinstance(payload, dict):
                records.append(deepcopy(payload))
        return records

    def clear(self, workspace_id: str | None = None) -> None:
        resolved = workspace_id or current_workspace_id()
        with self._lock, self._engine.begin() as connection:
            connection.execute(
                text("""
                DELETE FROM codegen_runs WHERE workspace_id = :workspace_id
            """),
                {"workspace_id": resolved},
            )
