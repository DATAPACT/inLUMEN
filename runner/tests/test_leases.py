from app.leases import WorkerLeases
from app.store import PipelineRunStore
from sqlalchemy import text


def test_only_one_worker_claims_a_run_and_quotas_cross_workers(tmp_path):
    store = PipelineRunStore(str(tmp_path / 'jobs.sqlite'))
    first, second = WorkerLeases(store._engine, 'test'), WorkerLeases(store._engine, 'test')
    assert first.claim('one', 'alice', 1, 2)
    assert not second.claim('one', 'alice', 1, 2)
    assert not second.claim('two', 'alice', 1, 2)
    assert second.claim('three', 'bob', 1, 2)
    assert not second.claim('four', 'carol', 1, 2)
    second.release('one', 'alice')
    assert first.active('one', 'alice')
    with store._engine.begin() as connection:
        connection.execute(text('UPDATE worker_leases SET expires_at=0'))
    assert not first.renew('one', 'alice')
    assert second.claim('one', 'alice', 1, 2)
    assert not first.renew('one', 'alice')
