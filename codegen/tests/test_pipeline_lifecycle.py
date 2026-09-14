import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.schemas import PipelineGenerationRun, PipelineGenerationRunStep


@pytest.fixture(autouse=True)
def clear_pipeline_job_state():
    main.clear_pipeline_job_state()
    main.PIPELINE_GENERATION_CACHE.clear()
    main.PIPELINE_GENERATION_TASKS.clear()
    yield
    for task in list(main.PIPELINE_GENERATION_TASKS.values()):
        task.cancel()
    main.PIPELINE_GENERATION_TASKS.clear()
    main.clear_pipeline_job_state()
    main.PIPELINE_GENERATION_CACHE.clear()


def pipeline_payload(name: str) -> dict:
    return {
        "context": {
            "pipeline": {"name": name},
            "graph": {
                "nodes": [
                    {
                        "flow_id": "ingest",
                        "label": "Data Ingestion",
                        "description": "Load input data.",
                        "type": "input",
                    },
                    {
                        "flow_id": "summary",
                        "label": "Summary",
                        "description": "Summarize ingested data.",
                        "type": "output",
                    },
                ],
                "edges": [{"source": "ingest", "target": "summary"}],
            },
            "runtime_constraints": {
                "allowed_packages": ["pandas", "numpy"],
            },
        },
        "options": {
            "validation_mode": "static",
            "allow_deterministic_fallback": True,
        },
    }


def wait_for_terminal_job(client: TestClient, run_id: str) -> dict:
    body = {}
    for _ in range(100):
        body = client.get(f"/v1/generate/pipeline-scripts/runs/{run_id}").json()
        if body["status"] in {
            "valid",
            "invalid",
            "failed",
            "cancelled",
        }:
            return body
        time.sleep(0.02)
    return body


def test_active_pipeline_job_can_be_cancelled(monkeypatch) -> None:
    cancelled_sandboxes = []

    async def slow_generation(
        _request,
        *,
        run_id,
        progress_callback,
        **_kwargs,
    ):
        run = PipelineGenerationRun(
            run_id=run_id,
            mode="pipeline_first_single_script",
            steps=[
                PipelineGenerationRunStep(
                    flow_id="ingest",
                    status="running",
                    stage="pipeline_generation",
                )
            ],
        )
        await progress_callback(run)
        await asyncio.sleep(3600)
        raise AssertionError("cancelled generation must not complete")

    monkeypatch.setattr(main, "generate_pipeline_script_bundles", slow_generation)
    monkeypatch.setattr(
        main,
        "cancel_sandbox_run",
        lambda run_id: cancelled_sandboxes.append(run_id),
    )

    with TestClient(main.app) as client:
        started = client.post(
            "/v1/generate/pipeline-scripts/runs",
            json=pipeline_payload("cancel-test"),
        )
        run_id = started.json()["run_id"]
        for _ in range(100):
            current = client.get(f"/v1/generate/pipeline-scripts/runs/{run_id}").json()
            if current.get("generation_run"):
                break
            time.sleep(0.01)

        cancelled = client.delete(f"/v1/generate/pipeline-scripts/runs/{run_id}")

    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["generation_run"]["status"] == "cancelled"
    assert body["generation_run"]["current_stage"] == "cancelled"
    assert body["generation_run"]["steps"][0]["stage"] == "cancelled"
    assert run_id not in main.PIPELINE_GENERATION_TASKS
    assert cancelled_sandboxes == [run_id]


def test_generation_history_cleanup_cancels_active_jobs(monkeypatch) -> None:
    cancelled_sandboxes = []

    async def slow_generation(
        _request,
        *,
        run_id,
        progress_callback,
        **_kwargs,
    ):
        run = PipelineGenerationRun(
            run_id=run_id,
            steps=[
                PipelineGenerationRunStep(
                    flow_id="ingest",
                    status="running",
                    stage="pipeline_generation",
                )
            ],
        )
        await progress_callback(run)
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "generate_pipeline_script_bundles", slow_generation)
    monkeypatch.setattr(
        main,
        "cancel_sandbox_run",
        lambda run_id: cancelled_sandboxes.append(run_id),
    )

    with TestClient(main.app) as client:
        started = client.post(
            "/v1/generate/pipeline-scripts/runs",
            json=pipeline_payload("clear-history-test"),
        )
        run_id = started.json()["run_id"]
        for _ in range(100):
            current = client.get(
                f"/v1/generate/pipeline-scripts/runs/{run_id}"
            ).json()
            if current.get("generation_run"):
                break
            time.sleep(0.01)

        cleared = client.delete("/v1/generate/pipeline-scripts/runs")
        remaining = client.get("/v1/generate/pipeline-scripts/runs").json()
        missing = client.get(f"/v1/generate/pipeline-scripts/runs/{run_id}")

    assert cleared.status_code == 200
    assert cleared.json() == {"status": "cleared", "deleted_count": 1}
    assert remaining == []
    assert missing.status_code == 404
    assert run_id not in main.PIPELINE_GENERATION_TASKS
    assert cancelled_sandboxes == [run_id]


def test_cancelled_status_wins_sandbox_shutdown_race(monkeypatch) -> None:
    async def generation_interrupted_by_teardown(
        _request,
        *,
        run_id,
        progress_callback,
        **_kwargs,
    ):
        run = PipelineGenerationRun(
            run_id=run_id,
            mode="pipeline_first_single_script",
            steps=[
                PipelineGenerationRunStep(
                    flow_id="ingest",
                    status="running",
                    stage="dependency_installation",
                )
            ],
        )
        await progress_callback(run)
        await asyncio.sleep(0.01)
        raise RuntimeError("sandbox container disappeared during cancellation")

    monkeypatch.setattr(
        main,
        "generate_pipeline_script_bundles",
        generation_interrupted_by_teardown,
    )
    monkeypatch.setattr(main, "cancel_sandbox_run", lambda _run_id: time.sleep(0.05))

    with TestClient(main.app) as client:
        started = client.post(
            "/v1/generate/pipeline-scripts/runs",
            json=pipeline_payload("cancel-race-test"),
        )
        run_id = started.json()["run_id"]
        for _ in range(100):
            current = client.get(f"/v1/generate/pipeline-scripts/runs/{run_id}").json()
            if current.get("generation_run"):
                break
            time.sleep(0.01)

        cancelled = client.delete(f"/v1/generate/pipeline-scripts/runs/{run_id}")

    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["generation_run"]["status"] == "cancelled"
    assert body["generation_run"]["steps"][0]["stage"] == "cancelled"
    assert body["error"] == "Generation cancelled by the user."


def test_matching_valid_pipeline_run_uses_cache(monkeypatch) -> None:
    original = main.generate_pipeline_script_bundles
    calls = []

    async def counted_generation(*args, **kwargs):
        calls.append(args[0])
        return await original(*args, **kwargs)

    monkeypatch.setattr(main, "generate_pipeline_script_bundles", counted_generation)
    payload = pipeline_payload("cache-test")

    with TestClient(main.app) as client:
        first = client.post(
            "/v1/generate/pipeline-scripts/runs",
            json=payload,
        )
        first_body = wait_for_terminal_job(client, first.json()["run_id"])
        second = client.post(
            "/v1/generate/pipeline-scripts/runs",
            json=payload,
        )
        second_body = wait_for_terminal_job(client, second.json()["run_id"])

    assert first_body["status"] == "valid"
    assert second_body["status"] == "valid"
    assert len(calls) == 1
    for body in (first_body, second_body):
        assert body["started_at"] is not None
        assert body["finished_at"] is not None
        assert body["duration_ms"] >= 0
        assert body["queue_duration_ms"] >= 0
    assert second_body["started_at"] >= first_body["finished_at"]
    assert {step["stage"] for step in second_body["generation_run"]["steps"]} == {
        "validated_cache_hit"
    }
    assert second_body["generation_run"]["stage_timings_ms"] == {
        "validated_cache_lookup": 0
    }
    assert second_body["generation_run"]["generation_usage"] == {
        "request_count": 0,
        "usage_reported_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }


def test_interrupted_durable_job_becomes_resumable_failure() -> None:
    request = main.GeneratePipelineScriptsRequest.model_validate(
        pipeline_payload("restart-test")
    )
    main.update_pipeline_job(
        "interrupted-run",
        status="running",
        request=request,
    )

    main.recover_interrupted_pipeline_jobs()

    recovered = main.PIPELINE_GENERATION_JOBS["interrupted-run"]
    assert recovered["status"] == "failed"
    assert "service restart" in recovered["error"]
    assert recovered["request"].context.pipeline["name"] == "restart-test"
    with TestClient(main.app) as client:
        response = client.post("/v1/generate/pipeline-scripts/runs/interrupted-run/resume", json={})
        assert response.status_code == 202
        resumed = wait_for_terminal_job(client, response.json()["run_id"])
    assert resumed["status"] == "valid"
    assert resumed["resumed_from_run_id"] == "interrupted-run"
    assert resumed["duration_ms"] >= 0
    assert resumed["timing_interrupted"] is False
    assert main.PIPELINE_JOB_STORE.get("interrupted-run")["duration_ms"] is None


@pytest.mark.parametrize("status", ["valid", "invalid", "failed", "cancelled"])
def test_duration_is_fixed_and_survives_store_reopen(monkeypatch, tmp_path, status):
    from app.job_store import PipelineJobStore
    path = str(tmp_path / "timing.sqlite3")
    monkeypatch.setattr(main, "PIPELINE_JOB_STORE", PipelineJobStore(path))
    clock = ["2026-09-14T10:00:00Z"]
    monkeypatch.setattr(main, "utc_now_iso", lambda: clock[0])
    main.update_pipeline_job("measured", status="queued")
    clock[0] = "2026-09-14T10:00:02Z"
    main.update_pipeline_job("measured", status="running")
    clock[0] = "2026-09-14T10:00:05Z"
    main.update_pipeline_job("measured", status="running")
    clock[0] = "2026-09-14T10:00:12Z"
    main.update_pipeline_job("measured", status=status)
    clock[0] = "2026-09-14T10:01:00Z"
    main.update_pipeline_job("measured", error="cleanup does not extend duration")
    reopened = PipelineJobStore(path)
    main.PIPELINE_GENERATION_JOBS.clear()
    monkeypatch.setattr(main, "PIPELINE_JOB_STORE", reopened)
    with TestClient(main.app) as client:
        body = client.get("/v1/generate/pipeline-scripts/runs/measured").json()
        assert body["duration_ms"] == 10000
        assert body["queue_duration_ms"] == 2000
        assert body["started_at"] == "2026-09-14T10:00:02Z"
        assert body["finished_at"] == "2026-09-14T10:00:12Z"
        assert client.get("/v1/generate/pipeline-scripts/runs").json()[0]["duration_ms"] == 10000


def test_queued_cancellation_has_no_worker_duration(monkeypatch):
    clock = ["2026-09-14T10:00:00Z"]
    monkeypatch.setattr(main, "utc_now_iso", lambda: clock[0])
    main.update_pipeline_job("queued-cancel", status="queued")
    clock[0] = "2026-09-14T10:00:03Z"
    main.mark_pipeline_job_cancelled("queued-cancel")
    job = main.PIPELINE_JOB_STORE.get("queued-cancel")
    assert job["duration_ms"] is None
    assert job["queue_duration_ms"] == 3000
    assert job.get("started_at") is None


def test_restart_does_not_invent_worker_completion_time(monkeypatch):
    main.update_pipeline_job("interrupted", status="queued")
    main.update_pipeline_job("interrupted", status="running")
    main.recover_interrupted_pipeline_jobs()
    job = main.PIPELINE_JOB_STORE.get("interrupted")
    assert job["status"] == "failed"
    assert job["timing_interrupted"] is True
    assert job["duration_ms"] is None
    assert job["finished_at"] is not None


def test_legacy_job_does_not_derive_duration_from_updated_at():
    from app.schemas import PipelineGenerationJobResponse
    job = PipelineGenerationJobResponse.model_validate({
        "run_id": "legacy", "status": "valid",
        "created_at": "2026-09-14T10:00:00Z", "updated_at": "2026-09-14T11:00:00Z",
    })
    assert job.duration_ms is None
    assert job.queue_duration_ms is None
