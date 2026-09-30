import io
import json
import os
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from artifact_runtime import ArtifactContractError
from task_package_contract import EXAMPLE, AUTHORING_GUIDE, public_manifest, resolve_requirements, task_data_contract, validate_package
from task_packages import inspect_packages, read_packages

FIXTURES = Path(__file__).parent / 'fixtures/task-packages'


def fixture_zip(name='audio-nlp-original'):
    """Package reviewable fixture sources without storing generated ZIPs in Git."""
    root = FIXTURES / name
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                archive.writestr(path.relative_to(root).as_posix(), path.read_bytes())
    return out.getvalue()


def package(**files):
    return {'main.py': b'print("ok")\n', 'inlumen.task.json': json.dumps(EXAMPLE).encode(), **files}


def zipped(files, root='nodes/2'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name, content in files.items(): archive.writestr(root + '/' + name, content)
    return out.getvalue()


def graph():
    return {'nodes': [{'id': str(i), 'data': {'type': 'task', 'label': label}} for i, label in enumerate(['Speech-to-Text', 'Data Anonymization', 'NER Tagging', 'Sentiment Analysis'], 2)], 'edges': [{'source': str(i), 'target': str(i+1), 'targetHandle': 'input'} for i in range(2,5)]}


def test_user_zip_is_accepted_unchanged():
    report, contents = inspect_packages(fixture_zip(), graph())
    assert report['valid'], report['errors']
    assert len(report['packages']) == 4
    assert {p['node_id'] for p in report['packages']} == {'2','3','4','5'}
    assert [p['data_contract']['outputs'][0]['filename'] for p in report['packages']] == ['anonymized.json', 'entities.json', 'result.json', 'transcription.json']
    for files in contents.values():
        assert json.loads(files['inlumen.task.json'])['version'] == 1


def test_portable_packages_bind_to_different_graph_ids_and_ports():
    labels = ['Extract', 'Enrich', 'Transform', 'Summarize']
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zip_file:
        for index, label in enumerate(labels):
            metadata = {'version': 1, 'inputs': [], 'output': {
                'type': 'file', 'path': f'step-{index}.json', 'format': 'json',
                'schema': {'type': 'object', 'required': ['text'], 'properties': {'text': {'type': 'string'}}},
            }}
            zip_file.writestr(f'nodes/{label}/main.py', 'print("test fixture")\n')
            zip_file.writestr(f'nodes/{label}/inlumen.task.json', json.dumps(metadata))
    for workspace in range(20):
        ids = [f'workspace-{workspace}-opaque-{index * 7}' for index in range(4)]
        pipeline = {'nodes': [
            {'id': ident, 'data': {'type': 'task', 'label': label}}
            for ident, label in zip(ids, labels)
        ], 'edges': [
            {'source': ids[index], 'target': ids[index + 1], 'targetHandle': f'port-{index}'}
            for index in range(3)
        ]}
        mappings = dict(zip([f'nodes/{label}' for label in labels], ids))
        report, _packages = inspect_packages(archive.getvalue(), pipeline, mappings)
        assert report['valid'], report['errors']
        assert {item['node_id'] for item in report['packages']} == set(ids)


@pytest.mark.parametrize('metadata', [
    {'version': 1, 'output': {'type': 'file', 'path': '../result.json'}},
    {'version': 1, 'output': {'type': 'file', 'path': '*.json'}},
    {'version': 1, 'output': {'type': 'file', 'path': 'x'}, 'unknown': True},
    {'version': 1, 'output': {'type': 'many', 'path': 'x'}},
    {'version': 2, 'output': {'type': 'file', 'path': 'x'}},
    {'version': 1, 'output': {'type': 'file', 'path': 'x', 'schema': {'$ref': 'https://example.test/schema'}}},
    {'version': 1, 'output': {'type': 'file', 'path': 'x'}, 'data_contract': {}},
])
def test_invalid_manifests(metadata):
    with pytest.raises(ArtifactContractError): task_data_contract(metadata)


def test_portable_contract_roundtrip():
    contract = task_data_contract(EXAMPLE)
    contract['outputs'][0]['name'] = 'transcript'
    assert task_data_contract(public_manifest(contract)) == contract
    assert contract['outputs'][0]['format'] == 'json'


def test_package_syntax_helpers_and_includes():
    files = package(**{'helper/tool.py': b'def work():\n    return 1', 'requirements.txt': b'-r deps/base.txt\n-c deps/pins.txt', 'deps/base.txt': b'requests[socks]>=2; python_version>="3.11"\n', 'deps/pins.txt': b'urllib3<3\n'})
    valid = validate_package(files)
    assert 'requests[socks]' in valid['requirements'][0]
    assert valid['constraints'] == ['urllib3<3']
    files['helper/tool.py'] = b'def bad('
    with pytest.raises(ArtifactContractError, match='helper/tool.py'): validate_package(files)


@pytest.mark.parametrize('requirement', ['--index-url https://example.test', '-r missing.txt', '-e .', 'thing @ file:///tmp/thing', 'a==1\na==2'])
def test_unsupported_dependencies_are_errors(requirement):
    with pytest.raises(ArtifactContractError): resolve_requirements({'requirements.txt': requirement})


def test_named_wheel_markers_and_extras_are_preserved():
    url = 'https://example.test/model-1.0-py3-none-any.whl'
    result = resolve_requirements({'requirements.txt': f'{url}\nrequests[socks]>=2; python_version >= "3.11"'})
    assert result[0] == 'model @ ' + url
    assert 'python_version' in result[1] and '[socks]' in result[1]


def test_zip_safety_and_ambiguous_mapping():
    for name in ['../main.py', '/main.py', 'dir\\main.py']:
        with pytest.raises(ArtifactContractError): read_packages(zipped({name: b''}))
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        item=zipfile.ZipInfo('Task/main.py'); item.external_attr=(stat.S_IFLNK | 0o777)<<16
        archive.writestr(item,'/tmp/unsafe')
    with pytest.raises(ArtifactContractError): read_packages(out.getvalue())
    g=graph();g['nodes'][0]['data']['label']='Same';g['nodes'][1]['data']['label']='Same'
    data=zipped(package(),root='Same')
    report,_=inspect_packages(data,g)
    assert not report['valid']
    report,_=inspect_packages(data,g,{'Same':'2'})
    assert report['valid']


def test_producer_replacement_checks_existing_consumer():
    g=graph();g['nodes'][1]['data']['generated_artifact']={'data_contract':task_data_contract({'version':1,'inputs':[{'port':'input','type':'directory'}],'output':{'type':'file','path':'out.json'}})}
    report,_=inspect_packages(zipped(package()),g)
    assert not report['valid']
    assert 'mismatch' in report['errors'][0]['message']


def test_import_does_not_stage_invalid_or_stale_packages_and_commits_once():
    import inlumen_api as api
    from local_api_client import LocalApiResponse
    response=LocalApiResponse(content=json.dumps(graph()).encode(),status_code=200,headers={'ETag':'"rev1"'})
    ok=LocalApiResponse(content=b'{"imported":1}',status_code=200,headers={})
    data=zipped(package(**{'helper.py':b'VALUE=1'}))
    report,_=inspect_packages(data,graph())
    with patch.dict(os.environ,{'AUTH_ENABLED':'false'}), patch.object(api,'dispatch_graph_request',side_effect=lambda path,**kwargs: response if path=='neo4j_get_graph' else ok) as graph_api, patch.object(api,'dispatch_object_request',return_value=ok) as storage:
        client=api.app.test_client()
        def post(revision,blob=data):
            return client.post('/api/pipeline/task-packages/import',headers={'If-Match':revision},data={'file':(io.BytesIO(blob),'code.zip'),'digest':report['digest']})
        assert post('"stale"').status_code==409
        storage.assert_not_called()
        assert post('"rev1"',zipped({'main.py':b'x'})).status_code in {409,422}
        storage.assert_not_called()
        result=post('"rev1"')
        assert result.status_code==200,result.json
        assert storage.call_count==3
        commits=[call for call in graph_api.call_args_list if call.args[0]=='neo4j_replace_task_packages']
        assert len(commits)==1


def test_shared_guidance_matches_published_spec():
    root=Path(__file__).resolve().parents[2]
    guide=json.loads((root/'frontend/src/features/flow/taskPackageGuide.generated.json').read_text())
    assert guide['guide']==AUTHORING_GUIDE
    assert guide['example']==EXAMPLE


def test_original_json_stages_keep_anonymized_text_through_export_runner(tmp_path):
    import subprocess, sys
    from deployment_artifacts import _ARGO_PORT_RUNNER
    packages = read_packages(fixture_zip())
    source=tmp_path/'source';source.mkdir()
    original='Hello, my email is alice@example.com. Great service!'
    (source/'transcription.json').write_text(json.dumps({'records':[{'text':original}]}))
    declaration=task_data_contract({'version':1,'output':{'type':'file','path':'transcription.json'}})['outputs'][0]
    text=None
    for index,name in enumerate(['Data Anonymization','NER Tagging','Sentiment Analysis']):
        files=packages[name];root=tmp_path/f'package-{index}';root.mkdir()
        for filename,body in files.items(): (root/filename).write_bytes(body)
        contract=validate_package(files)['data_contract']
        output=tmp_path/f'output-{index}'
        binding={'source_dir':str(source),'artifact':declaration,'source_node':str(index),'connection_id':f'{index}->{index+1}','target_port':'input'}
        result=subprocess.run([sys.executable,'-c',_ARGO_PORT_RUNNER,json.dumps([sys.executable,str(root/'main.py')]),'["output"]','[]','[]','["output"]',json.dumps(contract),json.dumps([binding])],env={**os.environ,'PIPELINE_INPUT_DIR':str(tmp_path/f'input-{index}'),'PIPELINE_OUTPUT_DIR':str(output)},capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        source=output/'output';declaration=contract['outputs'][0]
        data=json.loads((source/declaration['filename']).read_text())
        current=data['records'][0]['text']
        assert 'alice@example.com' not in current
        assert current != original
        if text is not None: assert current==text
        text=current
        assert len(list(source.iterdir()))==1


def test_published_schema_fixtures_and_structured_errors():
    from jsonschema import Draft202012Validator
    from task_package_contract import TASK_SCHEMA
    root=Path(__file__).resolve().parents[2]
    fixtures=json.loads((root/'contracts/task-package-v1/fixtures.json').read_text())
    validator=Draft202012Validator(TASK_SCHEMA)
    for value in fixtures['valid']:
        validator.validate(value)
        task_data_contract(value)
    for value in fixtures['invalid']:
        assert list(validator.iter_errors(value))
    report,_=inspect_packages(zipped(package(**{'inlumen.task.json':json.dumps(fixtures['invalid'][2])})),graph())
    error=report['errors'][0]
    assert error['field']=='output/type'
    assert error['supplied']=='unknown'
    assert error['expected']==['file','directory']
    assert error['example']==EXAMPLE


def test_bundle_member_declarations_survive_public_roundtrip():
    public={'version':1,'output':{'type':'directory','path':'bundle','members':[{'type':'file','path':'metrics.json','schema':{'type':'object','required':['score']}}]}}
    contract=task_data_contract(public)
    restored=task_data_contract(public_manifest(contract))
    assert restored==contract
    assert restored['outputs'][0]['members'][0]['schema']['required']==['score']


def test_pinned_model_variants_survive_uploaded_runtime_and_export():
    from deployment_agents import _task_capability_contract
    from model_plans import FASTER_WHISPER_PLAN
    # Use the portable declaration generated by the same reviewed ASR plan.
    plan=FASTER_WHISPER_PLAN
    models=[{key:plan[key] for key in ['model_id','model_revision','adapter_id','model_variants','runtime_selection']}]
    metadata=public_manifest(task_data_contract(EXAMPLE),models=models)
    validate_package(package(**{'inlumen.task.json':json.dumps(metadata)}))
    capability=_task_capability_contract(metadata,{'execution':{},'input':{},'output':{}},{})
    assert capability['models'][0]['model_variants']==models[0]['model_variants']
    assert capability['models'][0]['runtime_selection']==models[0]['runtime_selection']


def test_download_preserves_helpers_includes_binary_assets_and_public_manifest():
    import inlumen_api as api
    from local_api_client import LocalApiResponse
    files=package(**{'helpers/tool.py':b'VALUE=1','assets/table.bin':b'\x00\xff', 'requirements.txt':b'-r deps/base.txt\n-c deps/pins.txt','deps/base.txt':b'requests>=2','deps/pins.txt':b'urllib3<3'})
    g=graph();g['nodes']=g['nodes'][:1];g['edges']=[]
    g['nodes'][0]['data']['file_buckets']=[{'filename':name,'bucket':'files-2','role':'code'} for name in files]
    graph_response=LocalApiResponse(content=json.dumps(g).encode(),status_code=200,headers={})
    def read(path,**kwargs):
        return LocalApiResponse(content=files[kwargs['params']['filename']],status_code=200,headers={})
    with patch.dict(os.environ,{'AUTH_ENABLED':'false'}),patch.object(api,'dispatch_graph_request',return_value=graph_response),patch.object(api,'dispatch_object_request',side_effect=read):
        result=api.app.test_client().get('/api/pipeline/task-packages/download')
    assert result.status_code==200,result.json
    assert read_packages(result.data)=={'nodes/Speech-to-Text--2':files}
    report,_=inspect_packages(result.data,g)
    assert report['valid'],report['errors']


def test_public_scripts_are_not_rewritten_as_legacy_function_or_cli_adapters():
    from deployment_agents import _task_io_contract
    contract,_=_task_io_contract('def run(inputs, output_dir, context):\n    return []',[],declared=EXAMPLE)
    assert contract['execution']['adapter']=='filesystem'


def test_known_source_representation_conflict_is_rejected():
    g=graph();g['nodes'].append({'id':'1','data':{'type':'source','files':[{'filename':'audio.wav','role':'data'}]}})
    g['edges'].append({'source':'1','target':'2','targetHandle':'input'})
    manifest={**EXAMPLE,'inputs':[{'port':'input','type':'directory'}]}
    report,_=inspect_packages(zipped(package(**{'inlumen.task.json':json.dumps(manifest)})),g)
    assert not report['valid']
    assert 'mismatch' in report['errors'][0]['message']


def test_unpinned_user_zip_reports_all_errors_and_preserves_task_matches():
    fixture = fixture_zip('audio-sentiment-unpinned')
    report, _ = inspect_packages(fixture, graph())
    assert not report['valid']
    assert [p['node_id'] for p in report['packages']] == ['2', '3']
    assert len(report['errors']) == 4
    for item in report['packages']:
        assert {e['field'] for e in item['errors']} == {'models/0/model_revision', 'output/kind'}
        revision = next(e for e in item['errors'] if e['field'].endswith('model_revision'))
        assert 'immutable commit' in revision['message']
        assert 'HfApi' in revision['hint']
        assert 'corrective example' not in revision['message']
    remapped, _ = inspect_packages(fixture, graph(), {'nodes/2': '4', 'nodes/3': '5'})
    assert [p['node_id'] for p in remapped['packages']] == ['4', '5']


def test_invalid_manifest_still_reports_mapping_errors():
    manifest = {**EXAMPLE, 'models': [{'model_id': 'org/model', 'model_revision': 'main'}]}
    data = zipped(package(**{'inlumen.task.json': json.dumps(manifest)}), root='unmatched')
    report, _ = inspect_packages(data, graph())
    assert {e['field'] for e in report['errors']} == {'mapping', 'models/0/model_revision'}


def test_invalid_manifest_cannot_bypass_duplicate_mapping():
    blob = fixture_zip('audio-sentiment-unpinned')
    report, _ = inspect_packages(blob, graph(), {'nodes/2': '2', 'nodes/3': '2'})
    assert not report['valid']
    assert any(e.get('field') == 'mapping' for e in report['errors'])


def test_actual_invalid_zip_never_stages_code():
    import inlumen_api as api
    from local_api_client import LocalApiResponse
    data = fixture_zip('audio-sentiment-unpinned')
    response = LocalApiResponse(content=json.dumps(graph()).encode(), status_code=200, headers={'ETag': '"rev1"'})
    report, _ = inspect_packages(data, graph())
    with patch.dict(os.environ, {'AUTH_ENABLED': 'false'}), patch.object(api, 'dispatch_graph_request', return_value=response), patch.object(api, 'dispatch_object_request') as storage:
        client = api.app.test_client()
        result = client.post('/api/pipeline/task-packages/import', headers={'If-Match': '"rev1"'}, data={'file': (io.BytesIO(data), 'code.zip'), 'digest': report['digest']})
    assert result.status_code == 422
    assert len(result.json['errors']) == 4
    storage.assert_not_called()


def test_conflict_during_commit_returns_revalidation_error():
    import inlumen_api as api
    from local_api_client import LocalApiResponse
    data = zipped(package())
    report, _ = inspect_packages(data, graph())
    graph_response = LocalApiResponse(content=json.dumps(graph()).encode(), status_code=200, headers={'ETag': '"rev1"'})
    conflict = LocalApiResponse(content=b'{"code":"graph_conflict"}', status_code=409, headers={})
    ok = LocalApiResponse(content=b'{}', status_code=200, headers={})
    with patch.dict(os.environ, {'AUTH_ENABLED': 'false'}), patch.object(api, 'dispatch_graph_request', side_effect=[graph_response, conflict]), patch.object(api, 'dispatch_object_request', return_value=ok):
        result = api.app.test_client().post('/api/pipeline/task-packages/import', headers={'If-Match': '"rev1"'}, data={'file': (io.BytesIO(data), 'code.zip'), 'digest': report['digest']})
    assert result.status_code == 409
    assert 'Validate the packages again' in result.json['error']
    assert 'code' not in result.json  # Handled by the review, not the canvas conflict UI.


def test_parallel_tasks_with_duplicate_labels_keep_distinct_identities():
    from task_packages import task_package_folder
    g = {'nodes': [
        {'id': 'audio', 'data': {'type': 'source', 'label': 'Audio'}},
        {'id': 'left--branch', 'data': {'type': 'task', 'label': 'Analyze audio'}},
        {'id': 'right/branch', 'data': {'type': 'task', 'label': 'Analyze audio'}},
    ], 'edges': [{'source': 'audio', 'target': target} for target in ['left--branch', 'right/branch']]}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        # Reverse order deliberately: archive order must not imply execution order.
        for node in reversed(g['nodes'][1:]):
            folder = task_package_folder(node['id'], node['data']['label'])
            for name, body in package().items(): archive.writestr(f'{folder}/{name}', body)
    report, _ = inspect_packages(stream.getvalue(), g)
    assert report['valid'], report['errors']
    assert {p['folder']: p['node_id'] for p in report['packages']} == {
        'nodes/Analyze audio--left%2D%2Dbranch': 'left--branch',
        'nodes/Analyze audio--right%2Fbranch': 'right/branch',
    }
    g['nodes'][1]['data']['label'] = 'Renamed analysis'
    report, _ = inspect_packages(stream.getvalue(), g)
    assert report['valid']
    assert next(p for p in report['packages'] if p['node_id'] == 'left--branch')['label'] == 'Renamed analysis'


@pytest.mark.parametrize(('label', 'identity', 'expected'), [
    ('Transcribe audio', '2', 'nodes/Transcribe audio--2'),
    (' Analyze / audio: [raw] ', 'branch--a/b', 'nodes/Analyze - audio- -raw--branch%2D%2Da%2Fb'),
    ('', '2', 'nodes/Task--2'),
    ('A' * 100, '2', 'nodes/' + 'A' * 64 + '--2'),
])
def test_readable_task_folder_names(label, identity, expected):
    from task_packages import task_package_folder
    assert task_package_folder(identity, label) == expected


def test_missing_explicit_task_identity_requires_mapping_instead_of_guessing():
    g = {'nodes': [{'id': 'new', 'data': {'type': 'task', 'label': 'Analyze audio'}}], 'edges': []}
    data = zipped(package(), root='nodes/Analyze audio--removed')
    report, _ = inspect_packages(data, g)
    assert not report['valid']
    assert report['packages'][0]['node_id'] is None
    report, _ = inspect_packages(data, g, {'nodes/Analyze audio--removed': 'new'})
    assert report['valid']
