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
    assert body["generation_run"]["steps"][0]["stage"] == "cancelled"
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
    assert {step["stage"] for step in second_body["generation_run"]["steps"]} == {
        "validated_cache_hit"
    }
    assert second_body["generation_run"]["stage_timings_ms"] == {
        "validated_cache_lookup": 0
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
