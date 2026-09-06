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
