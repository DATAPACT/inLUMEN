from __future__ import annotations

import os
import threading
from typing import Any, Protocol

from neo4j import GraphDatabase


class RunSummaryStore(Protocol):
    @property
    def configured(self) -> bool: ...

    def publish(self, summary: dict[str, Any]) -> bool: ...

    def close(self) -> None: ...


def _neo4j_credentials() -> tuple[str, str]:
    raw = os.getenv("NEO4J_AUTH", "neo4j/password").strip()
    if "/" not in raw:
        return "", ""
    username, password = raw.split("/", 1)
    return username.strip(), password.strip()


class Neo4jRunSummaryStore:
    """Persist concise terminal run summaries without logs or artifact bytes."""

    def __init__(
        self,
        *,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        auth_username, auth_password = _neo4j_credentials()
        self.uri = (
            uri
            if uri is not None
            else os.getenv("NEO4J_URI", "bolt://datapact-neo4j-db:7687")
        ).strip()
        self.username = username if username is not None else auth_username
        self.password = password if password is not None else auth_password
        self._driver = (
            GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
                connection_timeout=5,
            )
            if self.uri and self.username and self.password
            else None
        )
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self._driver is not None

    def publish(self, summary: dict[str, Any]) -> bool:
        if self._driver is None or not str(summary.get("pipeline_id") or "").strip():
            return False
        with self._driver.session() as session:
            self._ensure_schema(session)
            return bool(session.execute_write(self._write_summary, dict(summary)))

    def _ensure_schema(self, session) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            session.run(
                """
                CREATE CONSTRAINT pipeline_run_summary_id IF NOT EXISTS
                FOR (run:PIPELINE_RUN_SUMMARY)
                REQUIRE run.run_id IS UNIQUE
                """
            ).consume()
            self._schema_ready = True

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()

    @staticmethod
    def _write_summary(transaction, summary: dict[str, Any]) -> bool:
        record = transaction.run(
            """
            MERGE (run:PIPELINE_RUN_SUMMARY {run_id: $run_id})
            SET run.pipeline_uid = $pipeline_id,
                run.active_version_uid = $active_version_uid,
                run.snapshot_sha256 = $snapshot_sha256,
                run.bundle_sha256 = $bundle_sha256,
                run.status = $status,
                run.engine = $engine,
                run.execution_mode = $execution_mode,
                run.created_at = datetime($created_at),
                run.started_at = CASE
                  WHEN $started_at IS NULL THEN null ELSE datetime($started_at)
                END,
                run.finished_at = datetime($finished_at),
                run.duration_ms = $duration_ms,
                run.output_count = $output_count,
                run.error_code = $error_code,
                run.error_message = $error_message,
                run.resource_profile = $resource_profile,
                run.resource_cpu = $resource_cpu,
                run.resource_memory_bytes = $resource_memory_bytes
            WITH run
            MATCH (pipeline:PIPELINE {uid: $pipeline_id})
            MERGE (pipeline)-[:HAS_RUN_SUMMARY]->(run)
            WITH pipeline, run,
                 pipeline.last_run_finished_at IS NULL
                 OR run.finished_at >= pipeline.last_run_finished_at AS is_latest
            FOREACH (_ IN CASE WHEN is_latest THEN [1] ELSE [] END |
              SET pipeline.last_run_id = run.run_id,
                  pipeline.last_run_status = run.status,
                  pipeline.last_run_engine = run.engine,
                  pipeline.last_run_snapshot_sha256 = run.snapshot_sha256,
                  pipeline.last_run_started_at = run.started_at,
                  pipeline.last_run_finished_at = run.finished_at,
                  pipeline.last_run_duration_ms = run.duration_ms,
                  pipeline.last_run_output_count = run.output_count,
                  pipeline.last_run_error = run.error_message
            )
            WITH pipeline, run
            OPTIONAL MATCH (pipeline)-[:HAS_VERSION]->(version:PIPELINE_VERSION {
              uid: $active_version_uid
            })
            FOREACH (_ IN CASE WHEN version IS NULL THEN [] ELSE [1] END |
              MERGE (version)-[:HAS_RUN_SUMMARY]->(run)
            )
            RETURN pipeline.uid AS pipeline_uid
            """,
            **summary,
        ).single()
        return bool(record and record["pipeline_uid"])
