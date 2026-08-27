import asyncio
import base64
import io
import json
import zipfile

import pytest
from app.artifacts import PipelineArtifactStore
from app.manager import (
    PipelineRunCapacityError,
    PipelineRunConflict,
    PipelineRunManager,
)
from app.models import CreatePipelineRunRequest
from app.store import PipelineRunStore


class FakeDagsterExecutor:
    configured = True

    def __init__(self, *, delay: float = 0, succeeds: bool = True):
        self.delay = delay
        self.succeeds = succeeds
        self.cancelled: list[str] = []
        self.secrets_seen: dict[str, str] = {}
        self.files_seen: list[dict] = []

    async def execute(self, run_id, files, runtime_secrets):
        self.secrets_seen = dict(runtime_secrets)
        self.files_seen = [dict(item) for item in files]
        await asyncio.sleep(self.delay)
        if not self.succeeds:
            return {
                "ok": False,
                "validation_report": {
                    "errors": ["Dagster asset materialization failed."],
                    "dagster": {
                        "steps": [
                            {"name": "image_build", "output": "image ready"},
                            {
                                "name": "materialize",
                                "output": (
                                    "dagster - node_1_task - STEP_START - running\n"
                                    "dagster - node_1_task - STEP_FAILURE - failed\n"
                                    "RuntimeError: Node script node_1_task failed with exit code 1:\\n"
                                    "FileNotFoundError: No CSV file found directly in PIPELINE_INPUT_DIR"
                                ),
                            },
                        ]
                    },
                },
                "run_outputs": [],
            }
        return {
            "ok": True,
            "validation_report": {
                "ok": True,
                "dagster": {
                    "steps": [
                        {
                            "output": (
                                "materialized node-1 "
                                + runtime_secrets.get(
                                    "INLUMEN_SECRET_NODE_1_API_KEY", ""
                                )
                            )
                        }
                    ]
                },
            },
            "run_outputs": [
                {
                    "path": "outputs/node-1/result.csv",
                    "filename": "result.csv",
                    "content": base64.b64encode(b"city,temp\nOslo,12\n").decode(),
                    "content_encoding": "base64",
                    "content_type": "text/csv",
                    "size_bytes": 18,
                }
            ],
        }

    async def cancel(self, run_id):
        self.cancelled.append(run_id)


class ProgressDagsterExecutor(FakeDagsterExecutor):
    async def progress(self, run_id):
        return {
            "execution_id": run_id,
            "phase": "running_pipeline",
            "message": "Dagster is executing pipeline nodes.",
            "logs": (
                "dagster - node_1_task - STEP_START - running\n"
                "Node node_1_task is still running (15s elapsed)."
            ),
        }


class SecretFailureExecutor(FakeDagsterExecutor):
    async def execute(self, run_id, files, runtime_secrets):
        secret = runtime_secrets["INLUMEN_SECRET_NODE_1_API_KEY"]
        return {
            "ok": False,
            "validation_report": {
                "errors": ["Dagster pipeline execution failed."],
                "dagster": {
                    "steps": [
                        {
                            "name": "dagster_materialize",
                            "output": f"RuntimeError: remote rejected credential {secret}",
                        }
                    ]
                },
            },
            "run_outputs": [],
        }


class FakeRunSummaryStore:
    configured = True

    def __init__(self, *, failures: int = 0):
        self.failures = failures
        self.published: list[dict] = []

    def publish(self, summary):
        self.published.append(dict(summary))
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("Neo4j unavailable")
        return True

    def close(self):
        return None


def request(*, key: str = "request-1", label: str = "Task") -> CreatePipelineRunRequest:
    return CreatePipelineRunRequest.model_validate(
        {
            "snapshot": {
                "pipeline_id": "pipeline-1",
                "pipeline_version": "1.0",
                "graph": {
                    "nodes": [{"id": "node-1", "data": {"label": label}}],
                    "edges": [],
                },
                "bundle_files": [
                    {
                        "path": "run-spec.json",
                        "filename": "run-spec.json",
                        "content": "{}",
                    }
                ],
                "bundle_manifest": {"targets": {"dagster": True}},
            },
            "idempotency_key": key,
            "runtime_secrets": {"INLUMEN_SECRET_NODE_1_API_KEY": "secret"},
        }
    )


@pytest.mark.asyncio
async def test_background_run_executes_dagster_and_persists_real_outputs(tmp_path):
    path = tmp_path / "runs.sqlite3"
    executor = FakeDagsterExecutor()
    manager = PipelineRunManager(
        PipelineRunStore(str(path)), adapter="dagster", executor=executor
    )

    queued, created = await manager.start(request())

    assert created is True
    assert queued["status"] == "queued"
    assert queued["snapshot"]["bundle_sha256"].startswith("sha256:")
    assert not any(key.startswith("_") for key in queued)
    stored_record = manager.store.get(queued["run_id"])
    assert stored_record.get("_bundle_reference")
    assert "_bundle_files" not in stored_record
    assert ': "secret"' not in json.dumps(stored_record)
    await manager.tasks[queued["run_id"]]

    completed = PipelineRunManager(
        PipelineRunStore(str(path)), adapter="dagster", executor=executor
    ).get(queued["run_id"])
    assert completed is not None
    assert completed["status"] == "succeeded"
    assert completed["result"]["execution_mode"] == "background"
    assert completed["result"]["outputs"][0]["filename"] == "result.csv"
    assert executor.secrets_seen == {"INLUMEN_SECRET_NODE_1_API_KEY": "secret"}
    event_text = json.dumps(manager.events(queued["run_id"])["events"])
    assert "secret" not in event_text
    assert "[REDACTED]" in event_text
    assert manager.output(
        queued["run_id"], "outputs/node-1/result.csv"
    )[0].startswith(b"city,temp")
    assert manager.bundle_zip(queued["run_id"]).startswith(b"PK")


@pytest.mark.asyncio
async def test_terminal_summary_is_concise_and_retried_after_storage_failure(tmp_path):
    path = tmp_path / "runs.sqlite3"
    summary_store = FakeRunSummaryStore(failures=1)
    manager = PipelineRunManager(
        PipelineRunStore(str(path)),
        adapter="dagster",
        executor=FakeDagsterExecutor(),
        summary_store=summary_store,
    )
    queued, _created = await manager.start(request(key="summary-run"))
    await manager.tasks[queued["run_id"]]

    stored = manager.store.get(queued["run_id"])
    assert stored["_summary_publish_error"] == "Neo4j unavailable"
    assert "secret" not in json.dumps(summary_store.published[0])
    assert summary_store.published[0]["pipeline_id"] == "pipeline-1"
    assert summary_store.published[0]["status"] == "succeeded"
    assert summary_store.published[0]["output_count"] == 1

    restarted = PipelineRunManager(
        PipelineRunStore(str(path)),
        adapter="dagster",
        executor=FakeDagsterExecutor(),
        summary_store=summary_store,
    )
    assert await restarted.reconcile_interrupted() == 0
    retried = restarted.store.get(queued["run_id"])
    assert retried["_summary_published_at"]
    assert "_summary_published_at" not in restarted.get(queued["run_id"])
    assert len(summary_store.published) == 2


@pytest.mark.asyncio
async def test_execution_and_download_use_the_same_frozen_bundle_snapshot():
    executor = FakeDagsterExecutor(delay=0.02)
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"), adapter="dagster", executor=executor
    )
    submission = request(key="snapshot-equivalence")
    queued, _created = await manager.start(submission)
    submission.snapshot.bundle_files[0]["content"] = '{"mutated":true}'

    with zipfile.ZipFile(io.BytesIO(manager.bundle_zip(queued["run_id"]))) as archive:
        assert archive.read("run-spec.json") == b"{}"
    await manager.tasks[queued["run_id"]]

    assert executor.files_seen[0]["content"] == "{}"
    assert manager.get(queued["run_id"])["snapshot"]["bundle_sha256"] == queued[
        "snapshot"
    ]["bundle_sha256"]


@pytest.mark.asyncio
async def test_background_run_persists_live_node_heartbeat_progress():
    executor = ProgressDagsterExecutor(delay=0.08)
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"), adapter="dagster", executor=executor
    )
    queued, _created = await manager.start(request(key="progress-run"))

    await asyncio.sleep(0.03)
    running = manager.get(queued["run_id"])
    assert running["status"] == "running"
    assert running["progress"] == {
        "phase": "running_pipeline",
        "message": "Task is running and reporting heartbeats.",
        "active_node_id": "node_1_task",
        "active_node_name": "Task",
        "node_elapsed_seconds": 15,
        "heartbeat_at": running["progress"]["heartbeat_at"],
        "resource_profile": None,
        "resource_cpu": None,
        "resource_memory_bytes": None,
        "resource_reason": None,
        "queue_position": None,
    }
    assert running["progress"]["heartbeat_at"]
    assert any(
        event["type"] == "node.started"
        for event in manager.events(queued["run_id"])["events"]
    )

    await manager.tasks[queued["run_id"]]
    assert manager.get(queued["run_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_idempotency_is_bound_to_executable_bundle_snapshot():
    executor = FakeDagsterExecutor(delay=10)
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"), adapter="dagster", executor=executor
    )
    first, created = await manager.start(request())
    second, second_created = await manager.start(request())

    assert created is True
    assert second_created is False
    assert second["run_id"] == first["run_id"]
    changed = request(label="Changed")
    changed.snapshot.bundle_files[0]["content"] = '{"changed":true}'
    with pytest.raises(PipelineRunConflict):
        await manager.start(changed)
    manager.tasks[first["run_id"]].cancel()


@pytest.mark.asyncio
async def test_outstanding_run_capacity_is_bounded_without_rejecting_retries():
    executor = FakeDagsterExecutor(delay=0.05)
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"),
        adapter="dagster",
        executor=executor,
        max_outstanding_runs=2,
    )
    first, _created = await manager.start(request(key="capacity-1"))
    second, _created = await manager.start(request(key="capacity-2"))

    assert manager.capabilities()["available_run_slots"] == 0
    replay, replay_created = await manager.start(request(key="capacity-1"))
    assert replay_created is False
    assert replay["run_id"] == first["run_id"]
    with pytest.raises(PipelineRunCapacityError) as rejected:
        await manager.start(request(key="capacity-3"))
    assert rejected.value.limit == 2
    assert rejected.value.outstanding == 2

    await manager.cancel(first["run_id"])
    await manager.tasks[first["run_id"]]
    third, third_created = await manager.start(request(key="capacity-3"))
    assert third_created is True
    assert third["run_id"] not in {first["run_id"], second["run_id"]}
    await asyncio.gather(*manager.tasks.values())


@pytest.mark.asyncio
async def test_cancellation_signals_dagster_executor_and_finishes_cancelled():
    executor = FakeDagsterExecutor(delay=0.05)
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"), adapter="dagster", executor=executor
    )
    queued, _created = await manager.start(request())

    cancelling = await manager.cancel(queued["run_id"])
    assert cancelling is not None
    assert cancelling["status"] == "cancelling"
    assert executor.cancelled == [queued["run_id"]]
    await manager.tasks[queued["run_id"]]

    cancelled = manager.get(queued["run_id"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    again = await manager.cancel(queued["run_id"])
    assert again["status"] == "cancelled"


@pytest.mark.asyncio
async def test_clear_all_cancels_active_runs_and_purges_records_and_artifacts(tmp_path):
    executor = FakeDagsterExecutor(delay=10)
    store = PipelineRunStore(str(tmp_path / "runs.sqlite3"))
    manager = PipelineRunManager(
        store,
        adapter="dagster",
        executor=executor,
        artifact_store=PipelineArtifactStore(tmp_path / "artifacts"),
    )
    queued, _created = await manager.start(request())

    result = await manager.clear_all()

    assert result == {
        "removed_runs": 1,
        "cancelled_runs": 1,
        "removed_artifact_roots": 1,
    }
    assert executor.cancelled == [queued["run_id"]]
    assert manager.get(queued["run_id"]) is None
    assert manager.list() == []
    assert list(manager.artifact_store.root.iterdir()) == []


@pytest.mark.asyncio
async def test_dagster_failure_preserves_execution_log_and_error():
    executor = FakeDagsterExecutor(succeeds=False)
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"), adapter="dagster", executor=executor
    )
    queued, _created = await manager.start(request())
    await manager.tasks[queued["run_id"]]

    failed = manager.get(queued["run_id"])
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "dagster_execution_failed"
    assert failed["error"]["message"].startswith(
        "Task: FileNotFoundError: No CSV file found"
    )
    events = manager.events(queued["run_id"])["events"]
    assert any(event["type"] == "runtime.log" for event in events)
    assert any(
        event["type"] == "node.failed" and event["node_id"] == "node_1_task"
        for event in events
    )
    assert any(
        "No CSV file found" in str(event["message"])
        for event in events
        if event["type"] == "dagster.log"
    )


@pytest.mark.asyncio
async def test_failure_error_and_neo4j_summary_redact_runtime_secrets():
    summary_store = FakeRunSummaryStore()
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"),
        adapter="dagster",
        executor=SecretFailureExecutor(),
        summary_store=summary_store,
    )
    queued, _created = await manager.start(request(key="secret-failure"))
    await manager.tasks[queued["run_id"]]

    failed = manager.get(queued["run_id"])
    assert failed["status"] == "failed"
    assert "secret" not in failed["error"]["message"]
    assert "[REDACTED]" in failed["error"]["message"]
    assert "secret" not in json.dumps(summary_store.published)


@pytest.mark.asyncio
async def test_restart_reconciliation_cancels_orphaned_dagster_run(tmp_path):
    path = tmp_path / "runs.sqlite3"
    store = PipelineRunStore(str(path))
    record = {
        "schema_version": "inlumen.pipeline-run@1",
        "run_id": "orphaned",
        "status": "running",
        "engine": "dagster",
        "execution_mode": "background",
        "snapshot": {
            "snapshot_id": "sha256:" + "b" * 64,
            "graph_sha256": "sha256:" + "a" * 64,
            "bundle_sha256": "sha256:" + "b" * 64,
            "node_count": 1,
            "edge_count": 0,
        },
        "created_at": "2026-08-26T10:00:00Z",
        "updated_at": "2026-08-26T10:00:01Z",
        "event_cursor": 0,
        "_events": [],
        "_snapshot_graph": {"nodes": [{"id": "node-1"}], "edges": []},
        "_bundle_files": [],
        "_idempotency_key": None,
    }
    store.save(record)
    executor = FakeDagsterExecutor()
    manager = PipelineRunManager(
        PipelineRunStore(str(path)), adapter="dagster", executor=executor
    )

    assert await manager.reconcile_interrupted() == 1
    assert executor.cancelled == ["orphaned"]
    reconciled = manager.get("orphaned")
    assert reconciled["status"] == "failed"
    assert reconciled["error"]["code"] == "runner_restarted"
