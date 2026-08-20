import unittest
from unittest.mock import patch

import minio_api
import neo4j_api


class _Result:
    def __init__(self, record=None):
        self.record = record
        self.consumed = False

    def single(self):
        return self.record

    def consume(self):
        self.consumed = True
        return self


class _GraphSession:
    def __init__(self):
        self.queries = []

    def run(self, query, **_parameters):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "collect(DISTINCT toString(step.flow_id))" in normalized:
            return _Result({"flow_ids": ["source-1", "task-1"]})
        if "collect(DISTINCT toString(version.uid))" in normalized:
            return _Result({"version_uids": ["main", "reusable-v1"]})
        if "RETURN count(entity) AS entity_count" in normalized:
            return _Result({"entity_count": 12})
        if "RETURN count(event) AS provenance_event_count" in normalized:
            return _Result({"provenance_event_count": 3})
        return _Result()


class _Bucket:
    def __init__(self, name):
        self.name = name


class _Object:
    def __init__(self, object_name):
        self.object_name = object_name


class _MinioClient:
    def __init__(self):
        self.removed_objects = []
        self.removed_buckets = []

    def list_buckets(self):
        return [
            _Bucket("files-step-id-source-1"),
            _Bucket("pipeline-version-file-snapshots"),
            _Bucket("unrelated-service-data"),
        ]

    def list_objects(self, bucket_name, recursive=False):
        self.recursive = recursive
        return [_Object(f"{bucket_name}/artifact")]

    def remove_object(self, bucket_name, object_name):
        self.removed_objects.append((bucket_name, object_name))

    def remove_bucket(self, bucket_name):
        self.removed_buckets.append(bucket_name)


class WorkspaceClearTests(unittest.TestCase):
    def test_deep_clear_removes_every_neo4j_entity(self):
        session = _GraphSession()
        with patch.object(neo4j_api, "_delete_version_file_snapshots") as delete_snapshots:
            result = neo4j_api._deep_clear_workspace(session)

        self.assertEqual(["source-1", "task-1"], result["flow_ids"])
        self.assertEqual(["main", "reusable-v1"], result["version_uids"])
        self.assertEqual(12, result["entity_count"])
        self.assertEqual(3, result["provenance_event_count"])
        self.assertTrue(any("MATCH (entity) DETACH DELETE entity" in query for query in session.queries))
        self.assertEqual(
            [unittest.mock.call("main"), unittest.mock.call("reusable-v1")],
            delete_snapshots.call_args_list,
        )

    def test_object_cleanup_removes_only_workspace_buckets(self):
        client = _MinioClient()
        bucket_names = minio_api._workspace_bucket_names(client)
        for bucket_name in bucket_names:
            minio_api._remove_bucket_strict(client, bucket_name)

        self.assertEqual(
            ["files-step-id-source-1", "pipeline-version-file-snapshots"],
            bucket_names,
        )
        self.assertEqual(bucket_names, client.removed_buckets)
        self.assertNotIn("unrelated-service-data", client.removed_buckets)
        self.assertTrue(client.recursive)


if __name__ == "__main__":
    unittest.main()
