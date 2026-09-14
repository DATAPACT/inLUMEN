"""Run only with disposable Neo4j and MinIO, like the graph transaction suite."""
import json
import os
from pathlib import Path
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from neo4j_api import app
from minio_access import read_object_bytes
from project_package import build_package, parse_package
from subpipeline_reference import derive_subpipeline_interface, public_ports_for_interface


@unittest.skipUnless(os.getenv('RUN_NEO4J_INTEGRATION') == '1' and os.getenv('RUN_MINIO_INTEGRATION') == '1',
                     'requires disposable Neo4j and MinIO')
class ProjectPackageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.auth = patch.dict(os.environ, {'AUTH_ENABLED': 'false'})
        self.auth.start()
        self.client = app.test_client()
        self.graph = json.loads((Path(__file__).resolve().parents[2] / 'examples/order-summary/pipeline.json').read_text())
        self.code = b'import os\nprint(os.getenv("DEVICE", "cpu"))\n'
        self.graph['nodes'][1]['data']['files'] = [{'filename': 'main.py', 'role': 'code'}]
        self.package = build_package(self.graph, lambda *_: self.code)

    def tearDown(self):
        self.auth.stop()

    def graph_response(self):
        response = self.client.get('/neo4j_get_graph')
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def import_package(self, content=None, revision=None):
        return self.client.post('/neo4j_project_package', data=content or self.package,
                                content_type='application/zip', headers={'If-Match': revision or self.graph_response().headers['ETag']})

    def test_round_trip_reload_export_and_static_suggestions(self):
        before = self.graph_response()
        preview = self.client.post('/neo4j_project_package?preview=true', data=self.package)
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertEqual(self.graph_response().headers['ETag'], before.headers['ETag'])
        imported = self.import_package()
        self.assertEqual(imported.status_code, 200, imported.json)
        graph = self.graph_response().json
        self.assertEqual(len(graph['nodes']), 3)
        self.assertTrue(set(node['id'] for node in graph['nodes']).isdisjoint({'orders', 'summary', 'result'}))
        task = next(node for node in graph['nodes'] if node['data']['type'] == 'task')
        ref = task['data']['file_buckets'][0]
        self.assertEqual(read_object_bytes(ref['snapshot_bucket'], ref['snapshot_object']), self.code)
        self.assertEqual(task['data']['generated_artifact']['runtime_environment'][0]['name'], 'DEVICE')
        exported = self.client.get('/neo4j_project_package')
        self.assertEqual(exported.status_code, 200, exported.json)
        _, blobs, summary = parse_package(exported.data)
        self.assertEqual(summary['files'], 1)
        self.assertEqual(list(blobs.values()), [self.code])
        # A second import proves all references were recreated, rather than reused.
        again = self.import_package(exported.data)
        self.assertEqual(again.status_code, 200, again.json)
        new_graph = self.graph_response().json
        self.assertTrue(set(n['id'] for n in graph['nodes']).isdisjoint(n['id'] for n in new_graph['nodes']))

    def test_failed_storage_and_graph_writes_preserve_canvas(self):
        self.assertEqual(self.import_package().status_code, 200)
        before = self.graph_response()
        with patch('neo4j_api.upload_object', side_effect=RuntimeError('storage unavailable')):
            self.assertEqual(self.import_package().status_code, 500)
        self.assertEqual(self.graph_response().json, before.json)
        from neo4j_api import _sync_graph_to_session
        def fail_after_write(*args, **kwargs):
            _sync_graph_to_session(*args, **kwargs)
            raise RuntimeError('failure after graph write')
        with patch('neo4j_api._sync_graph_to_session', side_effect=fail_after_write):
            self.assertEqual(self.import_package().status_code, 500)
        after = self.graph_response()
        self.assertEqual(after.json, before.json)
        self.assertEqual(after.headers['ETag'], before.headers['ETag'])

    def test_conflicts_and_invalid_packages_write_nothing(self):
        stale = self.graph_response().headers['ETag']
        self.assertEqual(self.import_package().status_code, 200)
        before = self.graph_response()
        with patch('neo4j_api.upload_object') as upload:
            rejected = self.import_package(revision=stale)
            self.assertEqual(rejected.status_code, 409, rejected.json)
            upload.assert_not_called()
            invalid = self.import_package(b'not a package')
            self.assertEqual(invalid.status_code, 422)
            upload.assert_not_called()
        self.assertEqual(self.graph_response().json, before.json)

    def test_reusable_dependency_survives_import_with_name_collision(self):
        name = f'Portable-{uuid.uuid4()}'
        flat = json.loads(json.dumps(self.graph))
        flat['nodes'][1]['data']['files'] = []
        existing = self.client.post('/neo4j_reusable_pipelines', json={'name': name, 'graph': flat})
        self.assertEqual(existing.status_code, 200, existing.json)
        self.graph['nodes'][1]['data'] = {'type': 'subpipeline', 'label': 'Nested task',
            'ports': public_ports_for_interface(derive_subpipeline_interface(flat)), 'subpipeline': {
                'reference': {'pipeline_uid': 'foreign-definition', 'pipeline_name': name}, 'resolved_graph': flat,
            }}
        package = build_package(self.graph, lambda *_: self.code)
        imported = self.import_package(package)
        self.assertEqual(imported.status_code, 200, imported.json)
        graph = self.graph_response().json
        sub = next(n['data']['subpipeline'] for n in graph['nodes'] if n['data']['type'] == 'subpipeline')
        self.assertNotEqual(sub['reference']['pipeline_uid'], 'foreign-definition')
        self.assertIn('(import ', sub['reference']['pipeline_name'])
        self.assertEqual(len(sub['resolved_graph']['nodes']), 3)
        self.assertEqual(self.client.get('/neo4j_project_package').status_code, 200)

    def test_gateway_import_into_fresh_workspace_has_no_source_storage_dependency(self):
        from inlumen_api import app as gateway
        from workspace_store import local_principal
        from workspace_storage import bucket_belongs_to_workspace
        source = replace(local_principal(), workspace_id=f'package-source-{uuid.uuid4()}')
        destination = replace(local_principal(), workspace_id=f'package-destination-{uuid.uuid4()}')
        client = gateway.test_client()
        with patch('auth_middleware.local_principal', return_value=source):
            revision = client.get('/api/pipeline/graph').headers['ETag']
            imported = client.post('/api/pipeline/package', data=self.package, headers={'If-Match': revision})
            self.assertEqual(imported.status_code, 200, imported.json)
            exported = client.get('/api/pipeline/package')
            self.assertEqual(exported.status_code, 200, exported.json)
            self.assertIn('application/zip', exported.content_type)
        with patch('auth_middleware.local_principal', return_value=destination):
            before = client.get('/api/pipeline/graph')
            self.assertEqual(before.json['nodes'], [])
            imported = client.post('/api/pipeline/package', data=exported.data, headers={'If-Match': before.headers['ETag']})
            self.assertEqual(imported.status_code, 200, imported.json)
            self.assertIn('X-InLumen-Graph-Revision', imported.headers)
            graph = client.get('/api/pipeline/graph').json
            task = next(node for node in graph['nodes'] if node['data']['type'] == 'task')
            ref = task['data']['file_buckets'][0]
            self.assertTrue(bucket_belongs_to_workspace(ref['snapshot_bucket'], destination.workspace_id))
            self.assertFalse(bucket_belongs_to_workspace(ref['snapshot_bucket'], source.workspace_id))
            content = client.get('/api/files/content', query_string={'container_id': task['id'], 'filename': 'main.py'})
            self.assertEqual(content.status_code, 200, content.json)
            self.assertEqual(content.data, self.code)
