from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

LOCAL_WORKSPACE_ID = "local-workspace"


def _database_url(value: str) -> str:
    raw = str(value or "").strip()
    if raw == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    if "://" in raw:
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    path = Path(raw).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{path}"


class PipelineRunStore:
    """PostgreSQL-backed run storage with SQLite retained for unit tests."""

    def __init__(self, database_path: str):
        self.database_path = database_path
        self._lock = threading.RLock()
        url = _database_url(database_path)
        options = (
            {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
            if database_path == ":memory:"
            else {"pool_pre_ping": True}
        )
        self._engine = create_engine(url, **options)
        with self._engine.begin() as connection:
            connection.execute(
                text("""
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    snapshot_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, run_id),
                    UNIQUE (workspace_id, idempotency_key)
                )
            """)
            )
            connection.execute(
                text("""
                CREATE INDEX IF NOT EXISTS pipeline_runs_updated_idx
                ON pipeline_runs(workspace_id, updated_at DESC)
            """)
            )
            connection.execute(
                text("""
                CREATE INDEX IF NOT EXISTS pipeline_runs_status_idx
                ON pipeline_runs(workspace_id, status)
            """)
            )

    @staticmethod
    def _workspace(record: dict[str, Any] | None = None) -> str:
        return str((record or {}).get("workspace_id") or LOCAL_WORKSPACE_ID)

    def save(self, record: dict[str, Any]) -> None:
        workspace_id = self._workspace(record)
        record["workspace_id"] = workspace_id
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO pipeline_runs (
                    workspace_id, run_id, idempotency_key, snapshot_sha256,
                    status, created_at, updated_at, record_json
                ) VALUES (
                    :workspace_id, :run_id, :idempotency_key, :snapshot_sha256,
                    :status, :created_at, :updated_at, :record_json
                )
                ON CONFLICT(workspace_id, run_id) DO UPDATE SET
                    idempotency_key = excluded.idempotency_key,
                    snapshot_sha256 = excluded.snapshot_sha256,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    record_json = excluded.record_json
            """),
                {
                    "workspace_id": workspace_id,
                    "run_id": record["run_id"],
                    "idempotency_key": record.get("_idempotency_key"),
                    "snapshot_sha256": record["snapshot"]["graph_sha256"],
                    "status": record["status"],
                    "created_at": record["created_at"],
                    "updated_at": record["updated_at"],
                    "record_json": encoded,
                },
            )

    def get(
        self, run_id: str, workspace_id: str | None = None
    ) -> dict[str, Any] | None:
        where = "run_id = :run_id"
        parameters: dict[str, Any] = {"run_id": run_id}
        if workspace_id is not None:
            where += " AND workspace_id = :workspace_id"
            parameters["workspace_id"] = workspace_id
        with self._lock, self._engine.connect() as connection:
            row = connection.execute(
                text(f"SELECT record_json FROM pipeline_runs WHERE {where}"), parameters
            ).first()
        return self._decode(row)

    def get_by_idempotency_key(
        self, key: str, workspace_id: str = LOCAL_WORKSPACE_ID
    ) -> dict[str, Any] | None:
        with self._lock, self._engine.connect() as connection:
            row = connection.execute(
                text("""
                SELECT record_json FROM pipeline_runs
                WHERE workspace_id = :workspace_id AND idempotency_key = :key
            """),
                {"workspace_id": workspace_id, "key": key},
            ).first()
        return self._decode(row)

    def count_statuses(
        self, statuses: set[str], workspace_id: str | None = None
    ) -> int:
        if not statuses:
            return 0
        ordered = sorted(statuses)
        parameters: dict[str, Any] = {
            f"status_{index}": value for index, value in enumerate(ordered)
        }
        placeholders = ",".join(f":status_{index}" for index in range(len(ordered)))
        workspace_clause = ""
        if workspace_id is not None:
            workspace_clause = "workspace_id = :workspace_id AND "
            parameters["workspace_id"] = workspace_id
        with self._lock, self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT count(*) FROM pipeline_runs WHERE "
                    f"{workspace_clause}status IN ({placeholders})"
                ),
                parameters,
            ).first()
        return int(row[0] if row is not None else 0)

    def list(
        self,
        *,
        limit: int | None = 20,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parameters: dict[str, Any] = {}
        where = ""
        if workspace_id is not None:
            where = "WHERE workspace_id = :workspace_id"
            parameters["workspace_id"] = workspace_id
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT :limit"
            parameters["limit"] = min(max(int(limit), 1), 100)
        with self._lock, self._engine.connect() as connection:
            rows = connection.execute(
                text(f"""
                SELECT record_json FROM pipeline_runs
                {where}
                ORDER BY updated_at DESC, created_at DESC
                {limit_clause}
            """),
                parameters,
            ).fetchall()
        return [record for row in rows if (record := self._decode(row)) is not None]

    def clear(self, workspace_id: str | None = None) -> int:
        parameters: dict[str, Any] = {}
        where = ""
        if workspace_id is not None:
            where = "WHERE workspace_id = :workspace_id"
            parameters["workspace_id"] = workspace_id
        with self._lock, self._engine.begin() as connection:
            row = connection.execute(
                text(f"SELECT count(*) FROM pipeline_runs {where}"), parameters
            ).first()
            connection.execute(text(f"DELETE FROM pipeline_runs {where}"), parameters)
        return int(row[0] if row is not None else 0)

    @staticmethod
    def _decode(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        return deepcopy(payload) if isinstance(payload, dict) else None
