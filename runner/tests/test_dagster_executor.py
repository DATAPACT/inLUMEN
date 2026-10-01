import asyncio

import httpx
import pytest

from app.dagster_executor import CodegenDagsterExecutor


@pytest.mark.asyncio
async def test_background_receipt_and_control_traffic_use_async_authenticated_workspace_requests():
    calls = []
    async def handle(request):
        calls.append(request)
        assert request.headers['X-InLumen-Workspace-Id'] == 'workspace-a'
        assert request.headers['Authorization'] == 'Bearer private-key'
        if request.method == 'POST':
            assert request.url.params['background'] == 'true'
            return httpx.Response(202, json={'status': 'accepted'})
        if request.url.path.endswith('/result'):
            return httpx.Response(200, json={'status': 'completed', 'result': {'ok': True}})
        return httpx.Response(200, json={'active_runs': 2, 'queued_runs': 37})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        executor = CodegenDagsterExecutor(service_url='http://codegen', api_key='private-key', client=client).for_workspace('workspace-a')
        results = await asyncio.gather(executor.execute('run', [], {}), executor.workload(), executor.cancel('run'))
        assert results[0] == {'ok': True}
        assert results[1]['queued_runs'] == 37
        assert len(calls) == 4
