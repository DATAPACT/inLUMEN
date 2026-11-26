#!/usr/bin/env python3
"""Exercise atomic worker claims and deletion tombstones on disposable PostgreSQL."""
import argparse
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=['runner', 'codegen'])
    args = parser.parse_args()
    url = os.environ.get('TEST_POSTGRES_URL', '')
    if not url.startswith(('postgresql://', 'postgresql+psycopg://')):
        parser.error('TEST_POSTGRES_URL must identify an explicitly disposable PostgreSQL database')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / args.component))
    from app.leases import WorkerLeases
    if args.component == 'runner':
        from app.store import PipelineRunStore as Store
    else:
        from app.job_store import PipelineJobStore as Store
    store, other = Store(url), Store(url)
    workspace = 'integration-' + uuid.uuid4().hex
    kind = 'integration-' + uuid.uuid4().hex
    workers = [WorkerLeases(store._engine, kind), WorkerLeases(other._engine, kind)]
    run_id = 'same-run'
    try:
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda worker: worker.claim(run_id, workspace, 1, 1), workers))
        assert sorted(outcomes) == [False, True], outcomes
        assert not workers[0].claim('excess', workspace, 1, 1)
        assert not workers[1].claim('excess', workspace + '-other', 1, 1)
        record = {'run_id': run_id, 'workspace_id': workspace, 'status': 'running',
                  'snapshot': {'graph_sha256': 'sha256:' + 'a' * 64},
                  'created_at': '2026-09-06', 'updated_at': '2026-09-06'}
        store.save(record)
        store.clear(workspace)
        other.save({**record, 'status': 'succeeded'})
        assert other.get(run_id, workspace) is None
        print(f'{args.component}: concurrent ownership, shared quotas and deletion tombstones passed')
    finally:
        for worker in workers:
            worker.release(run_id, workspace)
        store._engine.dispose()
        other._engine.dispose()


if __name__ == '__main__':
    main()
