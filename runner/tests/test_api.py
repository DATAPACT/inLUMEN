import asyncio
import time

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


class OutputExecutor(BlockingExecutor):
    async def execute(self, run_id, files, runtime_secrets):
        return {
            "ok": True,
            "validation_report": {"ok": True},
            "run_outputs": [
                {
                    "path": "outputs/node-1/table.csv",
                    "filename": "table.csv",
                    "content": "a,b\n1,2\n3,4\n",
                    "content_encoding": "utf-8",
                    "content_type": "text/csv",
                }
            ],
        }


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


def test_api_serves_output_windows_inline(monkeypatch):
    monkeypatch.setenv("RUNNER_SERVICE_API_KEY", "test-token")
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"),
        adapter="dagster",
        executor=OutputExecutor(),
    )
    monkeypatch.setattr(main, "RUN_MANAGER", manager)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(main.app) as client:
        run_id = client.post(
            "/v1/pipeline-runs", headers=headers, json=request_payload()
        ).json()["run_id"]
        for _ in range(100):
            if client.get(f"/v1/pipeline-runs/{run_id}", headers=headers).json()["status"] == "succeeded":
                break
            time.sleep(0.02)
        url = f"/v1/pipeline-runs/{run_id}/outputs/outputs/node-1/table.csv"

        full = client.get(url, headers=headers)
        assert full.status_code == 200
        assert full.content == b"a,b\n1,2\n3,4\n"
        assert full.headers["content-disposition"].startswith("attachment;")

        window = client.get(
            url, headers=headers, params={"offset": 4, "length": 4, "disposition": "inline"}
        )
        assert window.content == b"1,2\n"
        assert window.headers["content-disposition"].startswith("inline;")
        assert window.headers["x-inlumen-output-size"] == "12"
        assert window.headers["x-inlumen-output-offset"] == "4"

        assert client.get(url, headers=headers, params={"disposition": "evil"}).status_code == 422
        assert client.get(url, headers=headers, params={"offset": 99}).content == b""
        assert client.get(url, headers=headers, params={"offset": -1}).status_code == 422


def test_api_events_include_canvas_flow_ids(monkeypatch):
    monkeypatch.setenv("RUNNER_SERVICE_API_KEY", "test-token")
    manager = PipelineRunManager(
        PipelineRunStore(":memory:"), adapter="dagster", executor=BlockingExecutor()
    )
    monkeypatch.setattr(main, "RUN_MANAGER", manager)
    headers = {"Authorization": "Bearer test-token"}
    payload = request_payload()
    payload["snapshot"]["bundle_manifest"]["nodes"] = [
        {"flow_id": "orders", "asset_key": "node_orders_csv"}
    ]
    with TestClient(main.app) as client:
        run_id = client.post("/v1/pipeline-runs", headers=headers, json=payload).json()["run_id"]
        record = manager.store.get(run_id)
        manager._append_event(record, "node.started", "running", "Orders started.", node_id="node_orders_csv")
        manager.store.save(record)
        events = client.get(f"/v1/pipeline-runs/{run_id}/events?after=0", headers=headers).json()["events"]
        client.delete(f"/v1/pipeline-runs/{run_id}", headers=headers)
    node_event = next(event for event in events if event["type"] == "node.started")
    assert node_event["flow_id"] == "orders"
