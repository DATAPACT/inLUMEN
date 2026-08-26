import asyncio

from fastapi.testclient import TestClient

from app import main
from app.manager import PipelineRunManager
from app.store import PipelineRunStore


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


def test_api_requires_private_service_auth(monkeypatch):
    monkeypatch.setenv("RUNNER_SERVICE_API_KEY", "test-token")
    with TestClient(main.app) as client:
        response = client.get("/v1/pipeline-runs")
    assert response.status_code == 401
