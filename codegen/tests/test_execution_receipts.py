import pytest
from app.execution_store import ExecutionStore
from app.job_store import PipelineJobStore
from app.main import scoped_execution_id
from app.generation_budget import GenerationBudget


def test_receipts_survive_restart_reject_conflicts_and_redact_secrets(tmp_path):
    path = str(tmp_path / 'jobs.sqlite')
    store = ExecutionStore(PipelineJobStore(path)._engine)
    payload = {'files': [{'content': 'pass'}], 'runtime_secrets': {'TOKEN': 'sensitive'}}
    assert store.start('run', payload, 60)
    assert not store.start('run', payload, 60)
    with pytest.raises(ValueError): store.start('run', {'files': []}, 60)
    store.finish('run', {'ok': True, 'logs': 'credential sensitive'}, payload['runtime_secrets'])
    restarted = ExecutionStore(PipelineJobStore(path)._engine)
    receipt = restarted.get('run')
    assert receipt['status'] == 'completed'
    assert receipt['result']['logs'] == 'credential [REDACTED]'
    assert scoped_execution_id('run', 'alice') != scoped_execution_id('run', 'bob')


def test_generation_request_and_reported_spend_budgets():
    budget = GenerationBudget(1, 5)
    budget.reserve()
    with pytest.raises(ValueError): budget.reserve()
    budget = GenerationBudget(12, 5, reported_cost_usd=5)
    with pytest.raises(ValueError): budget.reserve()


def test_redacted_artifacts_keep_valid_integrity_metadata(tmp_path):
    import base64
    import hashlib
    store = ExecutionStore(PipelineJobStore(str(tmp_path / 'jobs.sqlite'))._engine)
    store.start('run', {}, 60)
    for encoding, content in [('utf-8', 'secret'), ('base64', base64.b64encode(b'secret').decode())]:
        result = store.finish('run', {'files': [{'content': content, 'content_encoding': encoding,
            'sha256': 'old', 'size_bytes': 6}]}, {'TOKEN': 'secret'})
        item = result['files'][0]
        raw = base64.b64decode(item['content']) if encoding == 'base64' else item['content'].encode()
        assert raw == b'[REDACTED]'
        assert item['size_bytes'] == len(raw)
        assert item['sha256'] == 'sha256:' + hashlib.sha256(raw).hexdigest()
