import asyncio

from app import main
from app.manager import PipelineRunManager
from app.store import PipelineRunStore
from fastapi.testclient import TestClient


class BlockingExecutor:
    configured = True

    async def execute(self, run_id, files, runtime_secrets):
        await asyncio.sleep(10)
        return {"ok": True, "validation_report": {"ok": True}, "run_outputs": []}

    async def cancel(self, run_id):
        return None


def request_payload():
    return {
        "snapshot": {
            "graph": {"nodes": [{"id": "node-1"}], "edges": []},
            "bundle_files": [
                {"path": "run-spec.json", "filename": "run-spec.json", "content": "{}"}
            ],
            "bundle_manifest": {"targets": {"dagster": True}},
        },
        "idempotency_key": "api-test",
    }


def test_api_submits_lists_reads_events_downloads_and_cancels(monkeypatch):
    monkeypatch.setenv("RUNNER_SERVICE_API_KEY", "test-token")
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"),
        adapter="dagster",
        executor=BlockingExecutor(),
    )
    monkeypatch.setattr(main, "RUN_MANAGER", manager)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(main.app) as client:
        response = client.post(
            "/v1/pipeline-runs", headers=headers, json=request_payload()
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert client.get("/v1/pipeline-runs", headers=headers).json()["runs"]
        assert client.get(
            f"/v1/pipeline-runs/{run_id}", headers=headers
        ).status_code == 200
        events = client.get(
            f"/v1/pipeline-runs/{run_id}/events?after=0", headers=headers
        ).json()
        assert events["events"][0]["type"] == "run.queued"
        bundle = client.get(f"/v1/pipeline-runs/{run_id}/bundle", headers=headers)
        assert bundle.status_code == 200
        assert bundle.content.startswith(b"PK")
        cancelled = client.delete(f"/v1/pipeline-runs/{run_id}", headers=headers)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] in {"cancelling", "cancelled"}
        cleared = client.delete("/v1/pipeline-runs", headers=headers)
        assert cleared.status_code == 200
        assert cleared.json()["removed_runs"] == 1
        assert client.get("/v1/pipeline-runs", headers=headers).json()["runs"] == []


def test_api_requires_private_service_auth(monkeypatch):
    monkeypatch.setenv("RUNNER_SERVICE_API_KEY", "test-token")
    with TestClient(main.app) as client:
        response = client.get("/v1/pipeline-runs")
        clear_response = client.delete("/v1/pipeline-runs")
    assert response.status_code == 401
    assert clear_response.status_code == 401


def test_api_reports_bounded_run_capacity(monkeypatch):
    monkeypatch.setenv("RUNNER_SERVICE_API_KEY", "test-token")
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"),
        adapter="dagster",
        executor=BlockingExecutor(),
        max_outstanding_runs=1,
    )
    monkeypatch.setattr(main, "RUN_MANAGER", manager)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(main.app) as client:
        first = client.post(
            "/v1/pipeline-runs", headers=headers, json=request_payload()
        )
        second_payload = request_payload()
        second_payload["idempotency_key"] = "api-test-2"
        rejected = client.post(
            "/v1/pipeline-runs", headers=headers, json=second_payload
        )

        assert first.status_code == 202
        assert rejected.status_code == 429
        assert rejected.headers["Retry-After"] == "5"
        assert rejected.json()["detail"] == {
            "message": (
                "Run capacity is full (1/1). Wait for a run to finish or "
                "cancel an active run before launching another."
            ),
            "code": "pipeline_run_capacity_full",
            "limit": 1,
            "outstanding": 1,
        }
        capabilities = client.get(
            "/v1/pipeline-runs/capabilities", headers=headers
        ).json()
        assert capabilities["available_run_slots"] == 0
        client.delete("/v1/pipeline-runs", headers=headers)
