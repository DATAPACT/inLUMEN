from __future__ import annotations

import json
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .schemas import GeneratePipelineScriptsRequest


class PipelineJobStore:
    """Durable SQLite storage for background generation job snapshots."""

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
                CREATE TABLE IF NOT EXISTS pipeline_generation_jobs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS pipeline_generation_jobs_updated_idx
                ON pipeline_generation_jobs(updated_at DESC)
                """
            )
            self._connection.commit()

    @staticmethod
    def _serializable_job(job: dict[str, Any]) -> dict[str, Any]:
        payload = deepcopy(job)
        request = payload.get("request")
        if isinstance(request, BaseModel):
            payload["request"] = request.model_dump(mode="json")
        return payload

    @staticmethod
    def _hydrated_job(payload: dict[str, Any]) -> dict[str, Any]:
        job = deepcopy(payload)
        request = job.get("request")
        if isinstance(request, dict):
            try:
                job["request"] = GeneratePipelineScriptsRequest.model_validate(request)
            except ValidationError:
                # Historical jobs can contain an older provider/model schema.
                # Keep their result visible without blocking service startup;
                # requests that cannot be safely hydrated are not resumable.
                job["request"] = None
        return job

    def save(self, job: dict[str, Any]) -> None:
        run_id = str(job.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("A pipeline generation job requires run_id.")
        payload = self._serializable_job(job)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO pipeline_generation_jobs (
                    run_id, status, created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (
                    run_id,
                    str(job.get("status") or "queued"),
                    str(job.get("created_at") or ""),
                    str(job.get("updated_at") or ""),
                    encoded,
                ),
            )
            self._connection.commit()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload_json
                FROM pipeline_generation_jobs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return self._hydrated_job(payload) if isinstance(payload, dict) else None

    def load_all(self) -> dict[str, dict[str, Any]]:
        return {
            str(job["run_id"]): job
            for job in self.list(limit=None)
            if job.get("run_id")
        }

    def list(
        self,
        *,
        limit: int | None = 50,
        statuses: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        where = ""
        if statuses:
            normalized = sorted({str(status).strip().lower() for status in statuses})
            placeholders = ",".join("?" for _ in normalized)
            where = f"WHERE lower(status) IN ({placeholders})"
            parameters.extend(normalized)
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(max(1, int(limit)))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT payload_json
                FROM pipeline_generation_jobs
                {where}
                ORDER BY updated_at DESC, created_at DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        jobs: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if isinstance(payload, dict):
                jobs.append(self._hydrated_job(payload))
        return jobs

    def clear(self) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM pipeline_generation_jobs")
            self._connection.commit()
