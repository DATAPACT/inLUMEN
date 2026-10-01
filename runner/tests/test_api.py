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


def test_workload_is_authenticated_anonymous_and_degrades_when_worker_is_unavailable(monkeypatch):
    monkeypatch.setenv('RUNNER_SERVICE_API_KEY', 'test-token')
    class ObservedExecutor(BlockingExecutor):
        async def workload(self):
            return {'active_runs': 2, 'queued_runs': 37, 'max_active_runs': 2, 'observed_at': '2026-10-01T12:00:00Z'}
    manager = PipelineRunManager(PipelineRunStore(':memory:'), adapter='dagster', executor=ObservedExecutor())
    monkeypatch.setattr(main, 'RUN_MANAGER', manager)
    main._WORKLOAD_CACHE.clear()
    with TestClient(main.app) as client:
        assert client.get('/v1/pipeline-runs/workload').status_code == 401
        response = client.get('/v1/pipeline-runs/workload', headers={'Authorization': 'Bearer test-token'})
        assert response.status_code == 200
        assert response.json()['queued_runs'] == 37
        assert not set(response.json()).intersection({'workspace_id', 'run_id', 'user_id'})
    main._WORKLOAD_CACHE.clear()
    manager.executor = BlockingExecutor()
    with TestClient(main.app) as client:
        response = client.get('/v1/pipeline-runs/workload', headers={'Authorization': 'Bearer test-token'})
        assert response.json()['worker_available'] is False
        assert 'active_runs' not in response.json()


def test_workload_outage_is_cached_instead_of_retrying_for_every_browser(monkeypatch):
    monkeypatch.setenv('RUNNER_SERVICE_API_KEY', 'test-token')
    calls = []
    class OfflineExecutor(BlockingExecutor):
        async def workload(self):
            calls.append(1)
            raise RuntimeError('unavailable')
    monkeypatch.setattr(main, 'RUN_MANAGER', PipelineRunManager(PipelineRunStore(':memory:'), adapter='dagster', executor=OfflineExecutor()))
    main._WORKLOAD_CACHE.clear()
    with TestClient(main.app) as client:
        for _ in range(39):
            response = client.get('/v1/pipeline-runs/workload', headers={'Authorization': 'Bearer test-token'})
            assert response.status_code == 200
            assert response.json()['worker_available'] is False
    assert len(calls) == 1
    main._WORKLOAD_CACHE.clear()
