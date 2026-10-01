import asyncio
import threading
import uuid

import httpx

from app import main
from app.resource_policy import ResourceAdmissionController
from test_deployment_validation_api import minimal_bundle_files


def test_workload_snapshot_preserves_fifo_and_exposes_no_other_run_ids():
    controller = ResourceAdmissionController()
    controller.enqueue('private-a')
    controller.enqueue('private-b')
    assert controller.snapshot('private-b')['queue_position'] == 2
    assert controller.snapshot()['queued_runs'] == 2
    assert 'private-' not in str(controller.snapshot('private-b'))
    controller.release('private-a')
    assert controller.snapshot('private-b')['queue_position'] == 1


def test_39_background_workers_do_not_block_workload_or_cancellation(monkeypatch):
    asyncio.run(_exercise_39_workers(monkeypatch))


async def _exercise_39_workers(monkeypatch):
    release = threading.Event()
    lock = threading.Lock()
    started = 0
    executions = []
    controller = ResourceAdmissionController()
    monkeypatch.setattr(main, 'RESOURCE_ADMISSION', controller)
    monkeypatch.setattr(main, 'cancel_deployment_execution', lambda execution_id: None)
    monkeypatch.setattr(main, 'cancel_sandbox_run', lambda execution_id: None)

    def validate(*args, **kwargs):
        nonlocal started
        with lock:
            started += 1
            executions.append(kwargs['execution_id'])
        assert release.wait(10)
        return {'ok': True, 'validation_report': {'ok': True}}

    monkeypatch.setattr(main, 'validate_deployment_bundle_files', validate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        payloads = [{'execution_id': str(uuid.uuid4()), 'files': minimal_bundle_files(),
                     'targets': {'dagster': True, 'argo': False}, 'validate_dagster': True} for _ in range(39)]
        try:
            responses = await asyncio.gather(*(client.post('/v1/validate/deployment-bundle?background=true', json=p) for p in payloads))
            assert all(response.status_code == 202 for response in responses)
            for _ in range(100):
                if started == 39:
                    break
                await asyncio.sleep(.01)
            assert started == 39
            workload = await asyncio.wait_for(client.get('/v1/execution-workload'), 1)
            assert workload.json()['queued_runs'] == 39
            cancelled = await asyncio.wait_for(client.delete(f"/v1/validate/deployment-bundle/{payloads[-1]['execution_id']}"), 1)
            assert cancelled.status_code == 200
            receipt = await asyncio.wait_for(client.get(f"/v1/validate/deployment-bundle/{payloads[0]['execution_id']}/result"), 1)
            assert receipt.json()['status'] == 'running'
        finally:
            release.set()
            await asyncio.gather(*list(main.DEPLOYMENT_TASKS.values()))
        duplicate = await client.post('/v1/validate/deployment-bundle?background=true', json=payloads[0])
        assert duplicate.status_code == 200
        assert len(executions) == 39
        assert controller.snapshot()['queued_runs'] == 0
