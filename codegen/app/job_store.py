from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from .schemas import GeneratePipelineScriptsRequest

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


class PipelineJobStore:
    """PostgreSQL-backed generation jobs, scoped by workspace."""

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
                CREATE TABLE IF NOT EXISTS pipeline_generation_jobs (
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, run_id)
                )
            """)
            )
            connection.execute(
                text("""
                CREATE INDEX IF NOT EXISTS pipeline_generation_jobs_updated_idx
                ON pipeline_generation_jobs(workspace_id, updated_at DESC)
            """)
            )

    @staticmethod
    def _serializable_job(job: dict[str, Any]) -> dict[str, Any]:
        payload = deepcopy(job)
        request = payload.get("request")
        if isinstance(request, BaseModel):
            payload["request"] = request.model_dump(mode="json")
        config = (
            payload.get("request", {}).get("llm_config")
            if isinstance(payload.get("request"), dict)
            else None
        )
        if isinstance(config, dict):
            config["api_key"] = ""
        return payload

    @staticmethod
    def _hydrated_job(payload: dict[str, Any]) -> dict[str, Any]:
        job = deepcopy(payload)
        request = job.get("request")
        if isinstance(request, dict):
            try:
                job["request"] = GeneratePipelineScriptsRequest.model_validate(request)
            except ValidationError:
                job["request"] = None
        return job

    def save(self, job: dict[str, Any]) -> None:
        run_id = str(job.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("A pipeline generation job requires run_id.")
        workspace_id = str(job.get("workspace_id") or LOCAL_WORKSPACE_ID)
        job["workspace_id"] = workspace_id
        encoded = json.dumps(
            self._serializable_job(job), ensure_ascii=False, separators=(",", ":")
        )
        with self._lock, self._engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO pipeline_generation_jobs (
                    workspace_id, run_id, status, created_at, updated_at, payload_json
                ) VALUES (
                    :workspace_id, :run_id, :status, :created_at, :updated_at, :payload_json
                )
                ON CONFLICT(workspace_id, run_id) DO UPDATE SET
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
            """),
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "status": str(job.get("status") or "queued"),
                    "created_at": str(job.get("created_at") or ""),
                    "updated_at": str(job.get("updated_at") or ""),
                    "payload_json": encoded,
                },
            )

    def get(
        self, run_id: str, workspace_id: str = LOCAL_WORKSPACE_ID
    ) -> dict[str, Any] | None:
        with self._lock, self._engine.connect() as connection:
            row = connection.execute(
                text("""
                SELECT payload_json FROM pipeline_generation_jobs
                WHERE workspace_id = :workspace_id AND run_id = :run_id
            """),
                {"workspace_id": workspace_id, "run_id": run_id},
            ).first()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        return self._hydrated_job(payload) if isinstance(payload, dict) else None

    def load_all(self) -> dict[str, dict[str, Any]]:
        return {
            str(job["run_id"]): job
            for job in self.list(limit=None, workspace_id=None)
            if job.get("run_id")
        }

    def list(
        self,
        *,
        limit: int | None = 50,
        statuses: set[str] | None = None,
        workspace_id: str | None = LOCAL_WORKSPACE_ID,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: dict[str, Any] = {}
        if workspace_id is not None:
            clauses.append("workspace_id = :workspace_id")
            parameters["workspace_id"] = workspace_id
        if statuses:
            normalized = sorted({str(status).strip().lower() for status in statuses})
            placeholders = []
            for index, status in enumerate(normalized):
                key = f"status_{index}"
                placeholders.append(f":{key}")
                parameters[key] = status
            clauses.append(f"lower(status) IN ({','.join(placeholders)})")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT :limit"
            parameters["limit"] = max(1, int(limit))
        with self._lock, self._engine.connect() as connection:
            rows = connection.execute(
                text(f"""
                SELECT payload_json FROM pipeline_generation_jobs
                {where}
                ORDER BY updated_at DESC, created_at DESC
                {limit_clause}
            """),
                parameters,
            ).fetchall()
        jobs = []
        for row in rows:
            payload = json.loads(str(row[0]))
            if isinstance(payload, dict):
                jobs.append(self._hydrated_job(payload))
        return jobs

    def clear(self, workspace_id: str | None = LOCAL_WORKSPACE_ID) -> None:
        parameters: dict[str, Any] = {}
        where = ""
        if workspace_id is not None:
            where = "WHERE workspace_id = :workspace_id"
            parameters["workspace_id"] = workspace_id
        with self._lock, self._engine.begin() as connection:
            connection.execute(
                text(f"DELETE FROM pipeline_generation_jobs {where}"), parameters
            )
