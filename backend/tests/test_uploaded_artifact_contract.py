"""Uploaded packages preserve explicit declarations through both exports."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from artifact_runtime import ArtifactContractError, CONTRACT_ID, validate_result
from async_runtime import run_async
from deployment_agents import generate_dockerfiles_with_agent
from deployment_artifacts import _ARGO_PORT_RUNNER, _parse_requirements_for_dagster_project, build_deployment_bundle_files
from task_artifacts import parse_task_metadata

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / 'examples' / 'uploaded-audio-pipeline'
NAMES = ['Speech-to-Text', 'Anonymization', 'NER Tagging', 'Sentiment Analysis']


def metadata(name='Speech-to-Text'):
    return json.loads((PACKAGES / name / 'inlumen.task.json').read_text())


@pytest.mark.parametrize('case', ['missing', 'two', 'unsafe', 'bad_schema', 'bad_members', 'version', 'representation'])
def test_invalid_uploaded_declarations_are_rejected(case):
    value = metadata()
    contract = value['data_contract']
    if case == 'missing': del value['data_contract']
    if case == 'two': contract['outputs'] *= 2
    if case == 'unsafe': contract['outputs'][0]['filename'] = '../escaped.json'
    if case == 'bad_schema': contract['outputs'][0]['schema'] = {'type': 'imaginary'}
    if case == 'bad_members': contract['outputs'][0]['members'] = [{}]
    if case == 'version': contract['version'] = '1'
    if case == 'representation': del contract['outputs'][0]['representation']
    with pytest.raises(ArtifactContractError):
        parse_task_metadata(json.dumps(value))


def test_metadata_upload_and_edit_are_validated_before_storage():
    import inlumen_api as api
    from local_api_client import LocalApiResponse
    graph = {'nodes': [{'id': '2', 'data': {'type': 'task'}}], 'edges': []}
    graph_response = LocalApiResponse(content=json.dumps(graph).encode(), status_code=200, headers={})
    storage_response = LocalApiResponse(content=b'{}', status_code=200, headers={})
    with patch.dict(os.environ, {'AUTH_ENABLED': 'false'}), \
         patch.object(api, 'dispatch_graph_request', return_value=graph_response), \
         patch.object(api, 'dispatch_object_request', return_value=storage_response) as storage, \
         patch.object(api, '_mark_codegen_artifact_user_modified', return_value={}) as modified:
        client = api.app.test_client()
        good = json.dumps(metadata()).encode()
        result = client.post('/api/nodes/2/files', data={'role': 'code', 'file': (io.BytesIO(good), 'inlumen.task.json')})
        assert result.status_code == 200, result.json
        assert storage.call_count == 1
        assert modified.call_count == 1
        storage.reset_mock()
        for content in ['{}', '{', json.dumps({'data_contract': {'contract_id': CONTRACT_ID, 'inputs': [], 'outputs': []}})]:
            result = client.post('/api/nodes/2/files', data={'role': 'code', 'file': (io.BytesIO(content.encode()), 'inlumen.task.json')})
            assert result.status_code == 422, result.json
            result = client.put('/api/nodes/2/files/text', json={'filename': 'inlumen.task.json', 'content': content})
            assert result.status_code == 422, result.json
        storage.assert_not_called()


def uploaded_payload():
    graph = {'nodes': [], 'edges': []}
    stored = {}
    for index, name in enumerate(NAMES, 2):
        files = []
        for path in sorted((PACKAGES / name).iterdir()):
            stored[(f'files-{index}', path.name)] = path.read_bytes()
            files.append({'filename': path.name, 'bucket': f'files-{index}', 'role': 'code'})
        graph['nodes'].append({'id': str(index), 'data': {'type': 'task', 'label': name, 'file_buckets': files}})
        if index > 2:
            graph['edges'].append({'source': str(index-1), 'target': str(index), 'sourceHandle': 'output', 'targetHandle': 'input'})
    async def read(bucket, filename):
        return stored[(bucket, filename)]
    with patch('deployment_agents.read_minio_object_bytes', side_effect=read):
        result = run_async(generate_dockerfiles_with_agent([], [], pipeline_graph=graph, require_attached_runtime=True))
    return graph, result.model_dump()


def test_uploaded_audio_packages_build_both_exports_without_codegen():
    graph, payload = uploaded_payload()
    assert len(payload['runtime_artifacts']) == 4
    for artifact in payload['runtime_artifacts']:
        assert artifact['generator'] == 'inlumen-attached-runtime'
        contract = artifact['data_contract']
        assert contract['contract_id'] == CONTRACT_ID
        assert len(contract['outputs']) == 1
        manifest = json.loads(next(f['content'] for f in artifact['files'] if f['filename'] == 'node-manifest.json'))
        assert manifest['data_contract'] == contract
    bundle = build_deployment_bundle_files(graph, payload, targets={'dagster': True, 'argo': True})
    assert bundle['manifest']
    requirements = [item['content'] for item in bundle['files'] if item['path'] in {'dagster/requirements.txt', 'argo/requirements.txt'}]
    assert len(requirements) == 2
    for content in requirements:
        assert 'en-core-web-sm @ https://github.com/explosion/spacy-models/' in content


def test_wheel_url_and_named_dependencies_are_preserved():
    wheel = 'https://example.test/my_model-1.2-py3-none-any.whl#sha256=abc'
    named = f'my-model @ {wheel}'
    assert _parse_requirements_for_dagster_project(wheel) == [named]
    assert _parse_requirements_for_dagster_project(named) == [named]


def test_uploaded_anonymizer_uses_bound_transcript_and_publishes_one_file(tmp_path):
    # Deliberately leave an unrelated, lexically earlier original transcript.
    # The package must follow its descriptor rather than discover it.
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'transcript.json').write_text(json.dumps({'text': 'Email alice@example.com. Great service!', 'segments': [{'text': 'alice@example.com'}]}))
    spec = metadata()['data_contract']['outputs'][0]
    (source / 'aaa-original.json').write_text(json.dumps({'text': 'WRONG INPUT'}))
    contract = metadata('Anonymization')['data_contract']
    binding = {'source_dir': str(source), 'artifact': spec, 'source_node': '2', 'connection_id': '2:output->3:input'}
    output = tmp_path / 'output'
    result = subprocess.run([
        sys.executable, '-c', _ARGO_PORT_RUNNER,
        json.dumps([sys.executable, str(PACKAGES / 'Anonymization' / 'main.py')]),
        '["output"]', '[]', '[]', '["output"]', json.dumps(contract), json.dumps([binding]),
    ], env={**os.environ, 'PIPELINE_INPUT_DIR': str(tmp_path / 'input'), 'PIPELINE_OUTPUT_DIR': str(output)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    published = output / 'output'
    assert len(validate_result(published, contract['outputs'])) == 1
    data = json.loads((published / 'anonymized.json').read_text())
    assert 'alice@example.com' not in json.dumps(data)
    assert '[EMAIL_1]' in data['text']
    assert 'segments' not in data
    assert data['anonymized'] is True
