import base64
import hashlib
import json
import os

import pytest
from app.job_store import PipelineJobStore
from app.llm import without_validation_bytes
from app.sandbox import fetch_configured_input, write_sample_inputs
from app.schemas import FileDescriptor, FileSample
from app.security import EXECUTION_WORKSPACE


def descriptor(content=b'value\nreal-row\n'):
    return FileDescriptor(filename='real.csv', bucket='files-ws-authenticated-1-hash', kind='table', format='csv',
        sample=FileSample(rows=[{'value': 'preview only'}], content_base64=base64.b64encode(content).decode(),
                          content_sha256=hashlib.sha256(content).hexdigest()))


def test_authenticated_staging_uses_complete_bytes_and_survives_serialization(tmp_path, monkeypatch):
    content = b'value\n' + b'real-row\n' * 3000
    item = FileDescriptor.model_validate_json(descriptor(content).model_dump_json())
    token = EXECUTION_WORKSPACE.set('authenticated-workspace')
    monkeypatch.setattr('app.sandbox.httpx.stream', lambda *a, **k: pytest.fail('must not call the user API'))
    try:
        for attempt in ('initial', 'retry'):
            inputs = tmp_path / attempt
            inputs.mkdir()
            write_sample_inputs(inputs / 'manifest.json', inputs, [item])
            assert (inputs / 'real.csv').read_bytes() == content
            assert 'content_base64' not in (inputs / 'manifest.json').read_text()
    finally:
        EXECUTION_WORKSPACE.reset(token)
    assert 'content_base64' not in json.dumps(without_validation_bytes({'nested': [item.model_dump()]}))


def test_missing_authenticated_bytes_fail_closed(tmp_path, monkeypatch):
    token = EXECUTION_WORKSPACE.set('authenticated-workspace')
    monkeypatch.setenv('CODEGEN_INPUT_FILE_BASE_URL', 'https://backend/api/files/content')
    monkeypatch.setenv('CODEGEN_INPUT_FILE_API_KEY', 'must-not-be-used')
    monkeypatch.setattr('app.sandbox.httpx.stream', lambda *a, **k: pytest.fail('credential must not be sent'))
    try:
        with pytest.raises(RuntimeError, match='not staged'):
            fetch_configured_input(tmp_path / 'input', FileDescriptor(filename='real.csv', bucket='files-ws-other'))
    finally:
        EXECUTION_WORKSPACE.reset(token)


def test_checksum_and_size_limit_fail(tmp_path, monkeypatch):
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    item = descriptor()
    item.sample.content_sha256 = '0' * 64
    with pytest.raises(RuntimeError, match='integrity'):
        write_sample_inputs(inputs / 'manifest.json', inputs, [item])
    assert not (inputs / 'real.csv').exists()
    monkeypatch.setenv('CODEGEN_INPUT_FILE_MAX_BYTES', '2')
    with pytest.raises(RuntimeError, match='exceeds'):
        write_sample_inputs(inputs / 'manifest.json', inputs, [descriptor()])


def test_local_development_without_downloader_remains_available(tmp_path, monkeypatch):
    token = EXECUTION_WORKSPACE.set('local-workspace')
    monkeypatch.delenv('CODEGEN_INPUT_FILE_BASE_URL', raising=False)
    try:
        assert not fetch_configured_input(tmp_path / 'input', FileDescriptor(filename='real.csv', bucket='files-step-id-1'))
    finally:
        EXECUTION_WORKSPACE.reset(token)


@pytest.mark.skipif(not os.getenv('AUTH_FILE_STAGED_INPUT'), reason='requires authenticated backend integration fixture')
def test_real_authenticated_backend_attachment_stages_in_codegen(tmp_path, monkeypatch):
    from pathlib import Path
    item = FileDescriptor.model_validate_json(Path(os.environ['AUTH_FILE_STAGED_INPUT']).read_text())
    token = EXECUTION_WORKSPACE.set('authenticated-workspace')
    monkeypatch.setattr('app.sandbox.httpx.stream', lambda *a, **k: pytest.fail('no user API download'))
    try:
        write_sample_inputs(tmp_path / 'manifest.json', tmp_path, [item])
        assert (tmp_path / 'real.csv').read_bytes() == b'value\nreplacement\n'
    finally:
        EXECUTION_WORKSPACE.reset(token)


def test_durable_job_retry_keeps_bytes_and_workspace_scope(tmp_path):
    from app.schemas import GeneratePipelineScriptsRequest
    request = GeneratePipelineScriptsRequest.model_validate({"context": {"graph": {
        "nodes": [{"flow_id": "1", "type": "source", "files": [descriptor().model_dump()]}], "edges": []}}})
    db = str(tmp_path / 'jobs.sqlite3')
    store = PipelineJobStore(db)
    store.save({'workspace_id': 'workspace-a', 'run_id': 'job', 'status': 'failed',
                'created_at': '2026-09-09T10:00:00Z', 'updated_at': '2026-09-09T10:00:00Z', 'request': request})
    reopened = PipelineJobStore(db)
    assert reopened.get('job', 'workspace-b') is None
    restored = reopened.get('job', 'workspace-a')['request'].context.graph.nodes[0].files[0]
    write_sample_inputs(tmp_path / 'manifest.json', tmp_path, [restored])
    assert (tmp_path / 'real.csv').read_bytes() == b'value\nreal-row\n'


@pytest.mark.skipif(not os.getenv('AUTH_FILE_STAGED_INPUT'), reason='requires authenticated backend integration fixture')
def test_production_service_auth_allows_staging_real_attachment(tmp_path, monkeypatch):
    from pathlib import Path
    from fastapi.testclient import TestClient
    import app.main as main
    from app.llm import LLMGenerationError

    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('CODEGEN_AUTH_DISABLED', 'false')
    monkeypatch.setenv('CODEGEN_SERVICE_API_KEY', 'isolated-service-key')
    monkeypatch.delenv('CODEGEN_SERVICE_API_KEY_FILE', raising=False)
    item = json.loads(Path(os.environ['AUTH_FILE_STAGED_INPUT']).read_text())
    async def stage_then_generate(request):
        assert EXECUTION_WORKSPACE.get() == 'authenticated-workspace'
        write_sample_inputs(tmp_path / 'manifest.json', tmp_path, request.context.graph.nodes[0].files)
        assert (tmp_path / 'real.csv').read_bytes() == b'value\nreplacement\n'
        # Stop at the next boundary: no paid model or generated program runs.
        raise LLMGenerationError('test reached model boundary after input staging')
    monkeypatch.setattr(main, 'generate_pipeline_script_bundles', stage_then_generate)
    payload = {'context': {'graph': {'nodes': [{'flow_id': '1', 'type': 'source', 'files': [item]}], 'edges': []}},
               'llm_config': {'provider': 'openrouter', 'model': 'test-model', 'base_url': 'https://model.invalid/v1'}}
    client = TestClient(main.app)
    endpoint = '/v1/generate/pipeline-scripts'
    assert client.post(endpoint, json=payload).status_code == 401
    headers = {'Authorization': 'Bearer isolated-service-key', 'X-InLumen-Workspace-Id': 'authenticated-workspace'}
    response = client.post(endpoint, json=payload, headers=headers)
    assert response.status_code == 422
    assert response.json()['detail'] == 'test reached model boundary after input staging'
    assert 'isolated-service-key' not in response.text
