from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import tempfile
import uuid
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol

from .artifacts import PipelineArtifactStore
from .models import CreatePipelineRunRequest
from .run_summaries import RunSummaryStore
from .store import PipelineRunStore

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"succeeded", "partial", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "preparing", "running", "cancelling"}
DEFAULT_MAX_OUTSTANDING_RUNS = 4
NODE_EVENT_PATTERN = re.compile(
    r"-\s+(?P<node>node_[A-Za-z0-9_]+)\s+-\s+"
    r"STEP_(?P<outcome>START|SUCCESS|FAILURE)\b"
)
PYTHON_ERROR_PATTERN = re.compile(
    r"^(?P<kind>FileNotFoundError|ModuleNotFoundError|ImportError|PermissionError|"
    r"ConnectionError|TimeoutError|OSError|ValueError|KeyError|TypeError|RuntimeError)"
    r":\s*(?P<message>.+)$",
    re.MULTILINE,
)
NODE_HEARTBEAT_PATTERN = re.compile(
    r"Node\s+(?P<node>node_[A-Za-z0-9_]+)\s+is still running\s+"
    r"\((?P<elapsed>\d+)s elapsed\)\."
)


class DagsterExecutor(Protocol):
    @property
    def configured(self) -> bool: ...

    async def execute(
        self,
        run_id: str,
        files: list[dict[str, Any]],
        runtime_secrets: dict[str, str],
    ) -> dict[str, Any]: ...

    async def cancel(self, run_id: str) -> None: ...

    async def progress(self, run_id: str) -> dict[str, Any]: ...


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def public_run_record(record: dict[str, Any]) -> dict[str, Any]:
    """Remove bundle contents, secrets, and worker bookkeeping from API output."""
    public = deepcopy(
        {key: value for key, value in record.items() if not key.startswith("_")}
    )
    snapshot = public.get("snapshot")
    if isinstance(snapshot, dict) and not snapshot.get("bundle_sha256"):
        snapshot["bundle_sha256"] = snapshot.get("graph_sha256")
    return public


class PipelineRunConflict(ValueError):
    pass


class PipelineRunCapacityError(RuntimeError):
    def __init__(self, *, limit: int, outstanding: int) -> None:
        self.limit = limit
        self.outstanding = outstanding
        super().__init__(
            f"Run capacity is full ({outstanding}/{limit}). "
            "Wait for a run to finish or cancel an active run before launching another."
        )


class PipelineRunManager:
    def __init__(
        self,
        store: PipelineRunStore,
        *,
        adapter: str = "disabled",
        executor: DagsterExecutor | None = None,
        artifact_store: PipelineArtifactStore | None = None,
        max_outstanding_runs: int = DEFAULT_MAX_OUTSTANDING_RUNS,
        summary_store: RunSummaryStore | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.executor = executor
        self.artifact_store = artifact_store or PipelineArtifactStore(
            tempfile.mkdtemp(prefix="inlumen-run-artifacts-")
        )
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._submission_lock = asyncio.Lock()
        self.max_outstanding_runs = max(int(max_outstanding_runs), 1)
        self.summary_store = summary_store

    @property
    def execution_available(self) -> bool:
        return bool(
            self.adapter == "dagster"
            and self.executor is not None
            and self.executor.configured
        )

    def capabilities(self) -> dict[str, Any]:
        enabled = self.execution_available
        outstanding = self._outstanding_run_count()
        return {
            "background_runs": True,
            "execution_available": enabled,
            "adapter": self.adapter,
            "execution_mode": "background" if enabled else "unavailable",
            "max_outstanding_runs": self.max_outstanding_runs,
            "outstanding_run_count": outstanding,
            "available_run_slots": max(self.max_outstanding_runs - outstanding, 0),
            "summary_persistence": bool(
                self.summary_store is not None and self.summary_store.configured
            ),
            "message": (
                "Runs execute the saved pipeline snapshot through Dagster and continue after the browser closes."
                if enabled
                else "Dagster background execution is not configured."
            ),
        }

    async def start(
        self, request: CreatePipelineRunRequest
    ) -> tuple[dict[str, Any], bool]:
        if not self.execution_available:
            raise RuntimeError("Dagster background execution is not configured.")
        async with self._submission_lock:
            return self._start_locked(request)

    def _start_locked(
        self, request: CreatePipelineRunRequest
    ) -> tuple[dict[str, Any], bool]:
        graph = request.snapshot.graph
        files = request.snapshot.bundle_files
        if not list(graph.get("nodes") or []):
            raise ValueError("Pipeline graph has no nodes.")
        if not files:
            raise ValueError("Executable Dagster snapshot has no files.")
        graph_sha256 = canonical_sha256(graph)
        bundle_sha256 = canonical_sha256(files)
        key = str(request.idempotency_key or "").strip() or None
        if key:
            existing = self.store.get_by_idempotency_key(key)
            if existing is not None:
                if existing["snapshot"]["bundle_sha256"] != bundle_sha256:
                    raise PipelineRunConflict(
                        "The idempotency key was already used for a different executable snapshot."
                    )
                return public_run_record(existing), False

        outstanding = self._outstanding_run_count()
        if outstanding >= self.max_outstanding_runs:
            raise PipelineRunCapacityError(
                limit=self.max_outstanding_runs,
                outstanding=outstanding,
            )

        nodes = list(graph.get("nodes") or [])
        edges = list(graph.get("edges") or [])
        now = utc_now_iso()
        run_id = uuid.uuid4().hex
        record: dict[str, Any] = {
            "schema_version": "inlumen.pipeline-run@1",
            "run_id": run_id,
            "status": "queued",
            "engine": "dagster",
            "execution_mode": "background",
            "snapshot": {
                "snapshot_id": bundle_sha256,
                "graph_sha256": graph_sha256,
                "bundle_sha256": bundle_sha256,
                "pipeline_id": request.snapshot.pipeline_id,
                "pipeline_version": request.snapshot.pipeline_version,
                "active_version_uid": request.snapshot.active_version_uid,
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "cancel_requested_at": None,
            "event_cursor": 0,
            "progress": {
                "phase": "queued",
                "message": "Waiting for the background runner.",
                "active_node_id": None,
                "active_node_name": None,
                "node_elapsed_seconds": None,
                "heartbeat_at": None,
                "resource_profile": None,
                "resource_cpu": None,
                "resource_memory_bytes": None,
                "resource_reason": None,
                "queue_position": None,
            },
            "error": None,
            "result": None,
            "_snapshot_graph": deepcopy(graph),
            "_bundle_reference": self.artifact_store.store_bundle(run_id, files),
            "_bundle_manifest": deepcopy(request.snapshot.bundle_manifest),
            "_runtime_secret_names": sorted(request.runtime_secrets),
            "_output_files": [],
            "_idempotency_key": key,
            "_events": [],
        }
        self._append_event(record, "run.queued", "queued", "Dagster run queued.")
        self.store.save(record)
        task = asyncio.create_task(
            self._execute_dagster(run_id, dict(request.runtime_secrets))
        )
        self.tasks[run_id] = task
        task.add_done_callback(lambda _task: self.tasks.pop(run_id, None))
        return public_run_record(record), True

    def _outstanding_run_count(self) -> int:
        return self.store.count_statuses(ACTIVE_STATUSES)

    def get(self, run_id: str) -> dict[str, Any] | None:
        record = self.store.get(run_id)
        return public_run_record(record) if record else None

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        records = [
            record
            for record in self.store.list(limit=None)
            if record.get("engine") != "contract-test"
        ]
        return [public_run_record(record) for record in records[:limit]]

    def events(self, run_id: str, *, after: int = 0) -> dict[str, Any] | None:
        record = self.store.get(run_id)
        if record is None:
            return None
        events = [
            event for event in record.get("_events", []) if int(event["id"]) > after
        ]
        return {
            "run_id": run_id,
            "events": deepcopy(events),
            "next_cursor": int(record.get("event_cursor") or 0),
        }

    def output(self, run_id: str, path: str) -> tuple[bytes, str, str] | None:
        record = self.store.get(run_id)
        if record is None:
            return None
        for entry in record.get("_output_files", []):
            if str(entry.get("path") or "") != path:
                continue
            storage_path = str(entry.get("_storage_path") or "")
            if storage_path:
                body = self.artifact_store.read_output(storage_path)
            else:
                content = str(entry.get("content") or "")
                body = (
                    base64.b64decode(content, validate=True)
                    if str(entry.get("content_encoding") or "") == "base64"
                    else content.encode("utf-8")
                )
            return (
                body,
                str(entry.get("content_type") or "application/octet-stream"),
                str(entry.get("filename") or PurePosixPath(path).name or "output"),
            )
        return None

    def bundle_zip(self, run_id: str) -> bytes | None:
        record = self.store.get(run_id)
        if record is None:
            return None
        files = self._bundle_files(record)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry in files:
                path = str(entry.get("path") or "").strip()
                relative = PurePosixPath(path)
                if not path or relative.is_absolute() or ".." in relative.parts:
                    continue
                content = str(entry.get("content") or "")
                body = (
                    base64.b64decode(content, validate=True)
                    if str(entry.get("content_encoding") or "") == "base64"
                    else content.encode("utf-8")
                )
                archive.writestr(str(relative), body)
        return buffer.getvalue()

    async def cancel(self, run_id: str) -> dict[str, Any] | None:
        record = self.store.get(run_id)
        if record is None:
            return None
        if record["status"] in TERMINAL_STATUSES:
            return public_run_record(record)
        now = utc_now_iso()
        record["status"] = "cancelling"
        record["cancel_requested_at"] = record.get("cancel_requested_at") or now
        record["updated_at"] = now
        self._append_event(
            record,
            "run.cancellation_requested",
            "cancelling",
            "Dagster cancellation requested.",
        )
        self.store.save(record)
        if self.executor is not None:
            try:
                await self.executor.cancel(run_id)
            except Exception as exc:  # noqa: BLE001 - keep lifecycle cancellable.
                latest = self.store.get(run_id) or record
                self._append_event(
                    latest,
                    "run.cancellation_warning",
                    "cancelling",
                    f"Cancellation signal failed: {exc}",
                )
                self.store.save(latest)
        if run_id not in self.tasks:
            self._finish_cancelled(self.store.get(run_id) or record)
        return public_run_record(self.store.get(run_id) or record)

    async def clear_all(self) -> dict[str, int]:
        """Cancel active work and purge all lifecycle records and artifacts."""
        async with self._submission_lock:
            records = self.store.list(limit=None)
            active_ids = [
                str(record.get("run_id") or "")
                for record in records
                if record.get("status") in ACTIVE_STATUSES
            ]
            if self.executor is not None:
                for run_id in active_ids:
                    try:
                        await self.executor.cancel(run_id)
                    except Exception:
                        # Clear all is authoritative. A stale remote execution
                        # must not preserve user-visible lifecycle metadata.
                        logger.warning(
                            "Failed to cancel execution %s during run purge.",
                            run_id,
                            exc_info=True,
                        )
            active_tasks = [self.tasks[run_id] for run_id in active_ids if run_id in self.tasks]
            for task in active_tasks:
                task.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
            removed_artifact_roots = self.artifact_store.clear()
            removed_runs = self.store.clear()
            return {
                "removed_runs": removed_runs,
                "cancelled_runs": len(active_ids),
                "removed_artifact_roots": removed_artifact_roots,
            }

    async def reconcile_interrupted(self) -> int:
        count = 0
        for record in self.store.list(limit=None):
            if record.get("status") not in ACTIVE_STATUSES:
                continue
            if self.executor is not None:
                try:
                    await self.executor.cancel(record["run_id"])
                except Exception as exc:  # noqa: BLE001 - recovery remains authoritative.
                    self._append_event(
                        record,
                        "run.recovery_warning",
                        record.get("status"),
                        f"Could not confirm remote cancellation during recovery: {exc}",
                    )
            now = utc_now_iso()
            record["status"] = "failed"
            record["updated_at"] = now
            record["finished_at"] = now
            record["error"] = {
                "code": "runner_restarted",
                "message": "The runner restarted while Dagster execution was active.",
            }
            record["result"] = self._terminal_result(
                record, "failed", error=record["error"]
            )
            self._append_event(
                record,
                "run.failed",
                "failed",
                "Active Dagster execution was cancelled during runner recovery.",
            )
            self._save_terminal_record(record)
            count += 1
        for record in self.store.list(limit=None):
            if (
                record.get("status") in TERMINAL_STATUSES
                and not record.get("_summary_published_at")
            ):
                self._publish_terminal_summary(record)
        return count

    async def _execute_dagster(
        self, run_id: str, runtime_secrets: dict[str, str]
    ) -> None:
        try:
            record = self.store.get(run_id)
            if record is None:
                return
            if record.get("status") == "cancelling":
                runtime_secrets.clear()
                self._finish_cancelled(record)
                return
            self._update_status(run_id, "preparing", "Executable snapshot accepted.")
            self._update_status(run_id, "running", "Dagster materialization started.")
            record = self.store.get(run_id)
            if record is None or self.executor is None:
                return
            progress_task = asyncio.create_task(self._track_live_progress(run_id))
            try:
                response = await self.executor.execute(
                    run_id,
                    self._bundle_files(record),
                    runtime_secrets,
                )
            finally:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            record = self.store.get(run_id)
            if record is None:
                runtime_secrets.clear()
                return
            root_failure = self._append_dagster_logs(
                record,
                response,
                secret_values=[value for value in runtime_secrets.values() if value],
            )
            runtime_secrets.clear()
            if record.get("status") == "cancelling":
                self._finish_cancelled(record)
                return
            validation_report = response.get("validation_report")
            validation_report = (
                validation_report if isinstance(validation_report, dict) else {}
            )
            if not response.get("ok"):
                errors = validation_report.get("errors") or [
                    "Dagster pipeline execution failed."
                ]
                message = root_failure or str(errors[0])
                error = {
                    "code": "dagster_execution_failed",
                    "message": message,
                    "details": {
                        "errors": (
                            [message, *errors]
                            if message not in errors
                            else errors
                        )
                    },
                }
                self._finish_failed(record, error)
                return

            outputs = response.get("run_outputs")
            output_files = [item for item in outputs or [] if isinstance(item, dict)]
            record["_output_files"] = self.artifact_store.store_outputs(
                run_id, output_files
            )
            now = utc_now_iso()
            record["status"] = "succeeded"
            record["updated_at"] = now
            record["finished_at"] = now
            record["progress"] = {
                **(record.get("progress") or {}),
                "phase": "completed",
                "message": "All pipeline nodes completed successfully.",
                "active_node_id": None,
                "active_node_name": None,
                "node_elapsed_seconds": None,
            }
            record["result"] = self._terminal_result(
                record,
                "succeeded",
                outputs=[self._output_metadata(item) for item in output_files],
            )
            self._append_event(
                record,
                "run.succeeded",
                "succeeded",
                f"Dagster completed with {len(output_files)} output artifact(s).",
            )
            self._save_terminal_record(record)
        except Exception as exc:  # noqa: BLE001 - adapter failures become run failures.
            runtime_secrets.clear()
            record = self.store.get(run_id)
            if record is None:
                return
            if record.get("status") == "cancelling":
                self._finish_cancelled(record)
                return
            self._finish_failed(
                record, {"code": "runner_error", "message": str(exc)}
            )

    async def _track_live_progress(self, run_id: str) -> None:
        if self.executor is None:
            return
        progress = getattr(self.executor, "progress", None)
        if not callable(progress):
            return
        while True:
            try:
                payload = await progress(run_id)
                if isinstance(payload, dict):
                    self._apply_live_progress(run_id, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient observation failure must never fail execution.
                logger.debug(
                    "Could not observe live progress for run %s.",
                    run_id,
                    exc_info=True,
                )
            await asyncio.sleep(2)

    def _apply_live_progress(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> None:
        record = self.store.get(run_id)
        if record is None or record.get("status") not in ACTIVE_STATUSES:
            return
        phase = str(payload.get("phase") or "running_pipeline").strip()
        message = str(payload.get("message") or "Dagster is working.").strip()
        observed_at = utc_now_iso()
        logs = str(payload.get("logs") or "").replace("\\n", "\n")
        emitted = {
            tuple(str(part) for part in item[:2])
            for item in record.get("_progress_node_events", [])
            if isinstance(item, list) and len(item) >= 2
        }
        active_node_id = str(
            (record.get("progress") or {}).get("active_node_id") or ""
        ).strip() or None
        events_changed = False
        for match in NODE_EVENT_PATTERN.finditer(logs):
            node_id = match.group("node")
            outcome = match.group("outcome").lower()
            key = (node_id, outcome)
            if key not in emitted:
                emitted.add(key)
                action = {
                    "start": "started",
                    "success": "completed",
                    "failure": "failed",
                }[outcome]
                event_status = (
                    "failed" if outcome == "failure" else "succeeded"
                    if outcome == "success" else "running"
                )
                self._append_event(
                    record,
                    f"node.{action}",
                    event_status,
                    f"{self._node_display_name(node_id)} {action}.",
                    node_id=node_id,
                )
                events_changed = True
            if outcome == "start":
                active_node_id = node_id
            elif active_node_id == node_id:
                active_node_id = None

        current_progress = record.get("progress") or {}
        heartbeat_matches = list(NODE_HEARTBEAT_PATTERN.finditer(logs))
        node_elapsed_seconds = current_progress.get("node_elapsed_seconds")
        heartbeat_at = current_progress.get("heartbeat_at")
        if heartbeat_matches:
            heartbeat = heartbeat_matches[-1]
            active_node_id = heartbeat.group("node")
            latest_elapsed = int(heartbeat.group("elapsed"))
            if latest_elapsed != node_elapsed_seconds:
                heartbeat_at = observed_at
            node_elapsed_seconds = latest_elapsed
            message = (
                f"{self._node_display_name(active_node_id)} is running and "
                "reporting heartbeats."
            )

        next_progress = {
            "phase": phase,
            "message": message,
            "active_node_id": active_node_id,
            "active_node_name": (
                self._node_display_name(active_node_id) if active_node_id else None
            ),
            "node_elapsed_seconds": node_elapsed_seconds if active_node_id else None,
            "heartbeat_at": heartbeat_at,
            "resource_profile": (
                payload.get("resource_profile")
                or current_progress.get("resource_profile")
            ),
            "resource_cpu": (
                payload.get("resource_cpu")
                if payload.get("resource_cpu") is not None
                else current_progress.get("resource_cpu")
            ),
            "resource_memory_bytes": (
                payload.get("resource_memory_bytes")
                if payload.get("resource_memory_bytes") is not None
                else current_progress.get("resource_memory_bytes")
            ),
            "resource_reason": (
                payload.get("resource_reason")
                or current_progress.get("resource_reason")
            ),
            "queue_position": payload.get("queue_position"),
        }
        if next_progress == current_progress and not events_changed:
            return
        record["progress"] = next_progress
        record["_progress_node_events"] = [list(item) for item in sorted(emitted)]
        record["updated_at"] = observed_at
        self.store.save(record)

    def _append_dagster_logs(
        self,
        record: dict[str, Any],
        response: dict[str, Any],
        *,
        secret_values: list[str],
    ) -> str:
        validation = response.get("validation_report")
        dagster = validation.get("dagster") if isinstance(validation, dict) else None
        steps = dagster.get("steps") if isinstance(dagster, dict) else None
        root_failure = ""
        emitted_node_events = {
            tuple(str(part) for part in item[:2])
            for item in record.get("_progress_node_events", [])
            if isinstance(item, list) and len(item) >= 2
        }
        for index, step in enumerate(steps or [], start=1):
            if not isinstance(step, dict):
                continue
            command = " ".join(str(part) for part in step.get("command") or [])
            output = str(step.get("output") or "").strip()
            redacted_output = output
            for secret_value in secret_values:
                redacted_output = redacted_output.replace(secret_value, "[REDACTED]")
            summary = (
                redacted_output[-12000:]
                if redacted_output
                else command or f"Dagster step {index}"
            )
            step_name = str(step.get("name") or "").strip()
            technical_type = (
                "dagster.log"
                if step_name in {"materialize", "dagster_materialize"}
                else "runtime.log"
            )
            self._append_event(record, technical_type, "running", summary)
            decoded_output = redacted_output.replace("\\n", "\n")
            for match in NODE_EVENT_PATTERN.finditer(decoded_output):
                node_id = match.group("node")
                outcome = match.group("outcome").lower()
                key = (node_id, outcome)
                if key in emitted_node_events:
                    continue
                emitted_node_events.add(key)
                event_status = (
                    "failed" if outcome == "failure" else "succeeded"
                    if outcome == "success" else "running"
                )
                action = {
                    "start": "started",
                    "success": "completed",
                    "failure": "failed",
                }[outcome]
                self._append_event(
                    record,
                    f"node.{action}",
                    event_status,
                    f"{self._node_display_name(node_id)} {action}.",
                    node_id=node_id,
                )
            failure = self._root_failure(decoded_output)
            if failure:
                failed_node_match = next(
                    (
                        match
                        for match in NODE_EVENT_PATTERN.finditer(decoded_output)
                        if match.group("outcome") == "FAILURE"
                    ),
                    None,
                )
                root_failure = (
                    f"{self._node_display_name(failed_node_match.group('node'))}: {failure}"
                    if failed_node_match
                    else failure
                )
        record["_progress_node_events"] = [
            list(item) for item in sorted(emitted_node_events)
        ]
        self.store.save(record)
        return root_failure

    def _save_terminal_record(self, record: dict[str, Any]) -> None:
        self.store.save(record)
        self._publish_terminal_summary(record)

    def _publish_terminal_summary(self, record: dict[str, Any]) -> None:
        if (
            self.summary_store is None
            or not self.summary_store.configured
            or record.get("_summary_published_at")
            or not str((record.get("snapshot") or {}).get("pipeline_id") or "").strip()
        ):
            return
        try:
            published = self.summary_store.publish(self._summary_payload(record))
            if not published:
                record["_summary_publish_error"] = (
                    "The captured pipeline was not found in summary storage."
                )
            else:
                record["_summary_published_at"] = utc_now_iso()
                record.pop("_summary_publish_error", None)
        except Exception as exc:
            record["_summary_publish_error"] = str(exc)[:600]
            logger.warning(
                "Could not persist terminal summary for run %s.",
                record.get("run_id"),
                exc_info=True,
            )
        self.store.save(record)

    @staticmethod
    def _summary_payload(record: dict[str, Any]) -> dict[str, Any]:
        snapshot = record.get("snapshot") or {}
        result = record.get("result") or {}
        error = record.get("error") or result.get("error") or {}
        progress = record.get("progress") or {}
        started_at = record.get("started_at")
        finished_at = record.get("finished_at")
        duration_ms = None
        if started_at and finished_at:
            try:
                started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                finished = datetime.fromisoformat(
                    str(finished_at).replace("Z", "+00:00")
                )
                duration_ms = max(int((finished - started).total_seconds() * 1000), 0)
            except ValueError:
                duration_ms = None
        return {
            "run_id": str(record.get("run_id") or ""),
            "pipeline_id": str(snapshot.get("pipeline_id") or ""),
            "active_version_uid": str(
                snapshot.get("active_version_uid") or "main"
            ),
            "snapshot_sha256": str(snapshot.get("graph_sha256") or ""),
            "bundle_sha256": str(snapshot.get("bundle_sha256") or ""),
            "status": str(record.get("status") or "failed"),
            "engine": str(record.get("engine") or "dagster"),
            "execution_mode": str(record.get("execution_mode") or "background"),
            "created_at": str(record.get("created_at") or finished_at),
            "started_at": str(started_at) if started_at else None,
            "finished_at": str(finished_at or record.get("updated_at")),
            "duration_ms": duration_ms,
            "output_count": len(result.get("outputs") or []),
            "error_code": str(error.get("code") or "") or None,
            "error_message": str(error.get("message") or "")[:600] or None,
            "resource_profile": str(progress.get("resource_profile") or "") or None,
            "resource_cpu": progress.get("resource_cpu"),
            "resource_memory_bytes": progress.get("resource_memory_bytes"),
        }

    @staticmethod
    def _node_display_name(node_id: str) -> str:
        name = re.sub(r"^node_\d+_?", "", node_id).replace("_", " ").strip()
        return name.title() or node_id

    @staticmethod
    def _root_failure(output: str) -> str:
        candidates = []
        for match in PYTHON_ERROR_PATTERN.finditer(output):
            kind = match.group("kind")
            message = match.group("message").strip().strip('"')
            if (
                not message
                or "Error occurred while executing op" in message
                or message.startswith("Node script ")
            ):
                continue
            candidates.append(f"{kind}: {message}")
        return candidates[-1][:600] if candidates else ""

    def _bundle_files(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        reference = str(record.get("_bundle_reference") or "")
        if reference:
            return self.artifact_store.load_bundle(reference)
        return [
            deepcopy(item)
            for item in record.get("_bundle_files", [])
            if isinstance(item, dict)
        ]

    def _update_status(self, run_id: str, status: str, message: str) -> None:
        record = self.store.get(run_id)
        if (
            record is None
            or record.get("status") in TERMINAL_STATUSES
            or record.get("status") == "cancelling"
        ):
            return
        now = utc_now_iso()
        record["status"] = status
        record["updated_at"] = now
        if status == "running" and not record.get("started_at"):
            record["started_at"] = now
        self._append_event(record, f"run.{status}", status, message)
        self.store.save(record)

    def _finish_cancelled(self, record: dict[str, Any]) -> None:
        now = utc_now_iso()
        record["status"] = "cancelled"
        record["updated_at"] = now
        record["finished_at"] = now
        record["progress"] = {
            **(record.get("progress") or {}),
            "phase": "cancelled",
            "message": "Pipeline execution was cancelled.",
            "active_node_id": None,
            "active_node_name": None,
            "node_elapsed_seconds": None,
        }
        record["result"] = self._terminal_result(record, "cancelled")
        self._append_event(record, "run.cancelled", "cancelled", "Dagster run cancelled.")
        self._save_terminal_record(record)

    def _finish_failed(self, record: dict[str, Any], error: dict[str, Any]) -> None:
        now = utc_now_iso()
        record["status"] = "failed"
        record["updated_at"] = now
        record["finished_at"] = now
        record["error"] = deepcopy(error)
        record["progress"] = {
            **(record.get("progress") or {}),
            "phase": "failed",
            "message": str(error["message"]),
            "active_node_id": None,
            "active_node_name": None,
            "node_elapsed_seconds": None,
        }
        record["result"] = self._terminal_result(record, "failed", error=error)
        self._append_event(record, "run.failed", "failed", str(error["message"]))
        self._save_terminal_record(record)

    @staticmethod
    def _output_metadata(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            key: entry.get(key)
            for key in (
                "path",
                "filename",
                "kind",
                "format",
                "content_type",
                "size_bytes",
                "sha256",
            )
            if entry.get(key) is not None
        }

    @staticmethod
    def _terminal_result(
        record: dict[str, Any],
        status: str,
        *,
        error: dict[str, Any] | None = None,
        outputs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result = {
            "schema_version": "inlumen.run-result@1",
            "run_id": record["run_id"],
            "status": status,
            "engine": record["engine"],
            "created_at": record["created_at"],
            "finished_at": record["finished_at"],
            "outputs": outputs or [],
            "execution_mode": record["execution_mode"],
        }
        if record.get("started_at"):
            result["started_at"] = record["started_at"]
        if error is not None:
            result["error"] = deepcopy(error)
        return result

    @staticmethod
    def _append_event(
        record: dict[str, Any],
        event_type: str,
        status: str | None,
        message: str | None,
        *,
        node_id: str | None = None,
    ) -> None:
        cursor = int(record.get("event_cursor") or 0) + 1
        record["event_cursor"] = cursor
        record.setdefault("_events", []).append(
            {
                "id": cursor,
                "timestamp": utc_now_iso(),
                "type": event_type,
                "status": status,
                "message": message,
                "node_id": node_id,
            }
        )
