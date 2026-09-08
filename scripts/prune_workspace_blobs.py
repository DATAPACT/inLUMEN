#!/usr/bin/env python3
"""Report aged, unreferenced generated/version blobs in one workspace.

Deletion is opt-in and requires all writers to that workspace to be stopped.
Ordinary uploaded files, runner outputs and execution receipts are never pruned.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


def referenced_objects(value):
    references = set()
    if isinstance(value, dict):
        for bucket_key, object_key in [('snapshot_bucket', 'snapshot_object'), ('bucket', 'filename')]:
            if isinstance(value.get(bucket_key), str) and isinstance(value.get(object_key), str):
                references.add((value[bucket_key], value[object_key]))
        for item in value.values():
            references.update(referenced_objects(item))
    elif isinstance(value, list):
        for item in value:
            references.update(referenced_objects(item))
    elif isinstance(value, str) and value.lstrip().startswith(('{', '[')):
        try:
            references.update(referenced_objects(json.loads(value)))
        except (ValueError, RecursionError):
            # An unreadable saved graph must prevent deletion, not lose its references.
            raise ValueError('Cannot determine references from stored JSON; repair it before pruning')
    return references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--older-than-days', type=int, default=30)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--writers-stopped', action='store_true')
    args = parser.parse_args()
    if args.older_than_days < 1:
        parser.error('Retention must be at least one day')
    if args.apply and not args.writers_stopped:
        parser.error('--apply requires --writers-stopped; stop all graph/codegen writers first')

    from neo4j import GraphDatabase
    from minio import Minio
    from runtime_config import get_neo4j_settings, get_minio_settings
    from workspace_queries import workspace_label
    from workspace_storage import workspace_bucket_prefix, version_snapshot_bucket

    uri, username, password = get_neo4j_settings()
    endpoint, access, secret, secure = get_minio_settings()
    objects = Minio(endpoint, access_key=access, secret_key=secret, secure=secure)
    with GraphDatabase.driver(uri, auth=(username, password)) as graph:
        with graph.session() as session:
            # The label is computed from a digest; no CLI text enters executable Cypher.
            rows = session.run(f'MATCH (n:{workspace_label(args.workspace)}) RETURN properties(n) AS properties')
            references = set()
            for row in rows:
                references.update(referenced_objects(row['properties']))

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
    version_bucket = version_snapshot_bucket(args.workspace)
    candidates = []
    for bucket in objects.list_buckets():
        if bucket.name != version_bucket and not bucket.name.startswith(workspace_bucket_prefix(args.workspace)):
            continue
        prefix = '' if bucket.name == version_bucket else '.generated/'
        for item in objects.list_objects(bucket.name, prefix=prefix, recursive=True):
            if item.last_modified and item.last_modified < cutoff and (bucket.name, item.object_name) not in references:
                candidates.append({'bucket': bucket.name, 'object': item.object_name, 'bytes': item.size})
    print(json.dumps({'workspace': args.workspace, 'action': 'delete' if args.apply else 'report', 'candidates': candidates}, indent=2))
    if args.apply:
        for item in candidates:
            objects.remove_object(item['bucket'], item['object'])


if __name__ == '__main__':
    main()
