"""Only run against an explicitly configured disposable Neo4j database."""
import os
import unittest
from unittest.mock import patch
from neo4j_api import app, _base_driver


@unittest.skipUnless(os.getenv('RUN_NEO4J_INTEGRATION') == '1', 'requires disposable Neo4j')
class GraphTransactionIntegrationTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        _base_driver.close()

    def test_stale_edit_is_rejected_and_transaction_rolls_back(self):
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            client = app.test_client()
            initial = client.get('/neo4j_get_graph')
            self.assertEqual(initial.status_code, 200, initial.json)
            revision = initial.headers['ETag']
            saved = client.post('/neo4j_sync_graph', headers={'If-Match': revision}, json={
                'graph': {'nodes': [{'id': 'integration-source', 'data': {'type': 'source', 'label': 'Original'}, 'position': {'x': 0, 'y': 0}}], 'edges': []},
            })
            self.assertEqual(saved.status_code, 200, saved.json)
            rejected = client.post('/neo4j_sync_graph', headers={'If-Match': revision}, json={'graph': {'nodes': [], 'edges': []}})
            self.assertEqual(rejected.status_code, 409, rejected.json)
            current = client.get('/neo4j_get_graph')
            self.assertEqual(len(current.json['nodes']), 1)
            self.assertEqual(current.headers['ETag'], saved.headers['ETag'])

    def test_publication_updates_file_pointers_atomically(self):
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            client = app.test_client()
            client.post('/neo4j_sync_graph', json={'graph': {'nodes': [
                {'id': 'task', 'data': {'type': 'task', 'label': 'Task'}, 'position': {'x': 0, 'y': 0}},
            ], 'edges': []}})
            original = {'filename': 'main.py', 'snapshot_object': '.generated/old/main.py'}
            response = client.post('/neo4j_update_generated_artifact', json={
                'flow_id': 'task', 'generated_artifact': {'publication_id': 'original'}, 'publish_files': [original],
            })
            self.assertEqual(response.status_code, 200, response.json)
            failed = client.post('/neo4j_update_generated_artifact', json={
                'flow_id': 'task', 'generated_artifact': {'publication_id': 'new'},
                'publish_files': [{'filename': 'other.py'}],
            })
            self.assertEqual(failed.status_code, 500)
            graph = client.get('/neo4j_get_graph').json
            data = graph['nodes'][0]['data']
            self.assertEqual(data['generated_artifact']['publication_id'], 'original')
            self.assertEqual(data['file_buckets'][0]['snapshot_object'], original['snapshot_object'])

    def test_internal_read_post_does_not_invalidate_browser_revision(self):
        from workspace_queries import INTERNAL_QUERY_CAPABILITY
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            client = app.test_client()
            initial = client.get('/neo4j_get_graph')
            read = client.post('/neo4j_run_query', json={
                'query': 'MATCH (s:STEP) RETURN count(s) AS count',
            }, environ_overrides={'inlumen.query_capability': INTERNAL_QUERY_CAPABILITY})
            self.assertEqual(read.status_code, 200, read.json)
            self.assertEqual(read.headers['ETag'], initial.headers['ETag'])
            saved = client.post('/neo4j_sync_graph', headers={'If-Match': initial.headers['ETag']},
                                json={'graph': {'nodes': [], 'edges': []}})
            self.assertEqual(saved.status_code, 200, saved.json)
            self.assertNotEqual(saved.headers['ETag'], initial.headers['ETag'])

    def test_auth_toggle_and_local_clear_preserve_private_graph(self):
        import neo4j_api
        from workspace_store import Principal, LOCAL_WORKSPACE_ID
        private = Principal('test-user', 'test-user', 'issuer', 'private-toggle-test', 'tenant', 'owner')
        private_label = neo4j_api._workspace_label(private.workspace_id)
        local_label = neo4j_api._workspace_label(LOCAL_WORKSPACE_ID)
        client = app.test_client()
        with patch('auth_middleware.validate_keycloak_bearer_token', return_value=({'sub': 'test-user'}, None)), \
             patch('auth_middleware.resolve_principal', return_value=private):
            with patch.dict('os.environ', {'AUTH_ENABLED': 'true'}):
                saved = client.post('/neo4j_sync_graph', json={'graph': {'nodes': [
                    {'id': 'private-drawing', 'data': {'type': 'source', 'label': 'Keep me'}, 'position': {'x': 12, 'y': 34}},
                ], 'edges': []}})
                self.assertEqual(saved.status_code, 200, saved.json)
                revision = saved.headers['ETag']
            # A fresh process runs legacy adoption when local mode starts.
            with patch.object(neo4j_api, '_legacy_graph_adopted', False), \
                 patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
                with _base_driver.session() as session:
                    session.run("CREATE (:STEP {flow_id:'unowned-legacy-test'})").consume()
                local = client.get('/neo4j_get_graph')
                self.assertEqual(local.status_code, 200, local.json)
                with _base_driver.session() as session:
                    self.assertEqual(session.run(f'MATCH(n:{private_label}:{local_label}) RETURN count(n) AS n').single()['n'], 0)
                    self.assertEqual(session.run(f"MATCH(n:STEP:{local_label} {{flow_id:'unowned-legacy-test'}}) RETURN count(n) AS n").single()['n'], 1)
                cleared = client.delete('/neo4j_clear_nodes')
                self.assertEqual(cleared.status_code, 200, cleared.json)
            with patch.dict('os.environ', {'AUTH_ENABLED': 'true'}):
                restored = client.get('/neo4j_get_graph')
                self.assertEqual(restored.status_code, 200, restored.json)
                self.assertEqual([node['id'] for node in restored.json['nodes']], ['private-drawing'])
                self.assertEqual(restored.headers['ETag'], revision)
                self.assertEqual(restored.json['nodes'][0]['position'], {'x': 12, 'y': 34})
