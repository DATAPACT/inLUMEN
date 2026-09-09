"""Only run against an explicitly configured disposable Neo4j database."""
import os
import json
import unittest
import uuid
from unittest.mock import patch
from neo4j_api import app, _base_driver, driver


@unittest.skipUnless(os.getenv('RUN_NEO4J_INTEGRATION') == '1', 'requires disposable Neo4j')
class GraphTransactionIntegrationTests(unittest.TestCase):
    def test_single_level_reusable_pipeline_save_attach_reload_and_legacy_nesting(self):
        from pipeline_graph_validation import validate_pipeline_graph
        from subpipeline_reference import public_ports_for_interface

        port = {"id": "data", "name": "data", "type": "Text", "required": True}
        source = {"id": "input", "data": {"type": "source", "label": "Input", "ports": {"inputs": [], "outputs": [port]}}}
        destination = {"id": "output", "data": {"type": "destination", "label": "Output", "ports": {"inputs": [port], "outputs": []}}}

        def edge(source_id, target_id):
            return {"id": f"{source_id}-{target_id}", "source": source_id, "target": target_id,
                    "sourceHandle": "data", "targetHandle": "data"}

        flat = {"nodes": [source, destination], "edges": [edge("input", "output")]}
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            client = app.test_client()
            saved = client.post('/neo4j_reusable_pipelines', json={"name": f"Flat-{uuid.uuid4()}", "graph": flat})
            self.assertEqual(saved.status_code, 200, saved.json)
            reference = saved.json['reference']
            interface = saved.json['interface']
            reusable_node = {"id": "reuse", "data": {"type": "subpipeline", "label": "Reusable",
                "ports": public_ports_for_interface(interface),
                "subpipeline": {"version": 2, "reference": reference, "interface": interface}}}
            parent = {"nodes": [source, reusable_node, destination], "edges": [edge("input", "reuse"), edge("reuse", "output")]}
            for version_uid in (reference['pipeline_uid'], 'nonexistent', 'self-reference'):
                attempted = json.loads(json.dumps(parent))
                attempted['nodes'][1]['data']['subpipeline']['reference']['version_uid'] = version_uid
                rejected = client.post('/neo4j_reusable_pipelines', json={"name": f"Nested-{uuid.uuid4()}", "graph": attempted})
                self.assertEqual(rejected.status_code, 422, rejected.json)
                self.assertEqual(rejected.json['code'], 'subpipeline-depth-exceeded')

            synced = client.post('/neo4j_sync_graph', json={"graph": parent})
            self.assertEqual(synced.status_code, 200, synced.json)
            attachment = {"flow_id": "reuse", "pipeline_uid": reference['pipeline_uid']}
            for dry_run in (True, False):
                attached = client.post('/neo4j_attach_reusable_pipeline_version', json={**attachment, "dry_run": dry_run})
                self.assertEqual(attached.status_code, 200, attached.json)
            loaded = client.get('/neo4j_get_graph').json
            resolved = next(node['data']['subpipeline'] for node in loaded['nodes'] if node['id'] == 'reuse')
            self.assertEqual(len(resolved['resolved_graph']['nodes']), 2)
            self.assertTrue(validate_pipeline_graph(loaded)['valid'])
            missing = client.post('/neo4j_attach_reusable_pipeline_version', json={**attachment, "pipeline_uid": "missing"})
            self.assertEqual(missing.status_code, 404)

            # Saved definitions reject updates and leave existing references unchanged.
            updated = json.loads(json.dumps(flat))
            updated['nodes'][0]['data']['label'] = 'Updated input'
            changed = client.post('/neo4j_reusable_pipelines', json={
                'pipeline_uid': reference['pipeline_uid'], 'name': saved.json['reference']['pipeline_name'], 'graph': updated,
            })
            self.assertEqual(changed.status_code, 409, changed.json)
            self.assertEqual(changed.json['code'], 'immutable-reusable-pipeline')
            refreshed = client.get('/neo4j_get_graph').json
            reused = next(node['data']['subpipeline'] for node in refreshed['nodes'] if node['id'] == 'reuse')
            self.assertEqual(reused['resolved_graph']['nodes'][0]['data']['label'], 'Input')
            with _base_driver.session() as session:
                count = session.run('MATCH (:PIPELINE {uid:$uid})-[:HAS_VERSION]->(v) RETURN count(v) AS n', uid=reference['pipeline_uid']).single()['n']
            self.assertEqual(count, 0)

            # Seed an invalid legacy definition, including a
            # recursive reference. No recursive expansion may occur on any read.
            with _base_driver.session() as session:
                session.run('MATCH (v:PIPELINE {uid:$uid}) SET v.graph_json=$graph',
                            uid=reference['pipeline_uid'], graph=json.dumps(parent)).consume()
            for dry_run in (True, False):
                rejected = client.post('/neo4j_attach_reusable_pipeline_version', json={**attachment, "dry_run": dry_run})
                self.assertEqual(rejected.status_code, 422, rejected.json)
            catalog = client.get('/neo4j_reusable_pipelines').json['pipelines']
            version = next(p for p in catalog if p['uid'] == reference['pipeline_uid'])
            self.assertIn('Only one subpipeline level', version['unavailable_reason'])
            loaded = client.get('/neo4j_get_graph').json
            unresolved = next(node['data']['subpipeline'] for node in loaded['nodes'] if node['id'] == 'reuse')
            self.assertNotIn('resolved_graph', unresolved)
            self.assertIn('Only one subpipeline level', unresolved['resolution_error'])
            self.assertFalse(validate_pipeline_graph(loaded)['valid'])
            # Compatibility reads preserve the original definition without rewriting it.
            version = client.get('/neo4j_reusable_pipeline_version', query_string=attachment).json
            self.assertEqual(len(version['graph']['nodes']), 3)
            with _base_driver.session() as session:
                stored = session.run('MATCH (v:PIPELINE {uid:$uid}) RETURN v.graph_json AS graph',
                                     uid=reference['pipeline_uid']).single()['graph']
            self.assertEqual(json.loads(stored), parent)

    def test_agent_reuse_works_without_version_identifiers(self):
        import asyncio
        import ast
        from pipeline_agent.tools import build_pipeline_editor_tools
        tools = {tool.__name__: tool for tool in build_pipeline_editor_tools()}
        port = {"id": "data", "name": "data", "type": "Text", "required": True}
        flat = {"nodes": [
            {"id": "input", "data": {"type": "source", "ports": {"inputs": [], "outputs": [port]}}},
            {"id": "output", "data": {"type": "destination", "ports": {"inputs": [port], "outputs": []}}},
        ], "edges": [{"source": "input", "target": "output", "sourceHandle": "data", "targetHandle": "data"}]}
        async def invoke(name, payload):
            return json.loads(ast.literal_eval(await tools[name](json.dumps(payload))))
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            client = app.test_client()
            created = asyncio.run(invoke('create_reusable_pipeline', {'name': f'Agent-{uuid.uuid4()}', 'graph': flat}))
            uid = created[0]['reusable_pipeline']['pipeline_uid']
            listing = asyncio.run(invoke('list_reusable_pipelines', {}))
            listed = next(row['reusable_pipeline'] for row in listing if row['reusable_pipeline']['pipeline_uid'] == uid)
            self.assertNotIn('version_uid', listed)
            client.post('/neo4j_sync_graph', json={'graph': {'nodes': [], 'edges': []}})
            asyncio.run(invoke('create_step', {'type': 'subpipeline', 'label': 'Reusable', 'reusable_pipeline_uid': uid}))
            loaded = client.get('/neo4j_get_graph').json
            node = next(node for node in loaded['nodes'] if node['data']['type'] == 'subpipeline')
            self.assertEqual(len(node['data']['subpipeline']['resolved_graph']['nodes']), 2)
            asyncio.run(invoke('configure_subpipeline_step', {'flow_id': node['id'], 'pipeline_uid': uid}))
            # Assistant-created definitions appear as one item in the normal UI catalog.
            self.assertTrue(any(item['uid'] == uid for item in client.get('/neo4j_reusable_pipelines').json['pipelines']))

    def test_legacy_references_remain_readable_and_cannot_be_edited_through_autosave(self):
        uid, first, second = (str(uuid.uuid4()) for _ in range(3))
        def graph(label):
            return {"nodes": [{"id": "input", "data": {"type": "source", "label": label}}], "edges": []}
        with driver.session() as session:
            session.run("""
                CREATE (p:PIPELINE {uid:$uid, status:'reusable', name:'Legacy', active_version_uid:$second})
                CREATE (a:PIPELINE_VERSION {uid:$first, graph_json:$a})
                CREATE (b:PIPELINE_VERSION {uid:$second, graph_json:$b})
                CREATE (p)-[:HAS_VERSION]->(a), (p)-[:HAS_VERSION]->(b)
            """, uid=uid, first=first, second=second, a=json.dumps(graph('Original')), b=json.dumps(graph('Latest'))).consume()
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            client = app.test_client()
            latest = client.get('/neo4j_reusable_pipeline', query_string={'pipeline_uid': uid})
            self.assertEqual(latest.json['graph']['nodes'][0]['data']['label'], 'Latest')
            pinned = client.get('/neo4j_reusable_pipeline_version', query_string={'pipeline_uid': uid, 'version_uid': first})
            self.assertEqual(pinned.json['graph']['nodes'][0]['data']['label'], 'Original')
            rejected = client.post('/neo4j_save_pipeline_active_version', json={'uid': first, 'name': 'Changed', 'graph': graph('Changed')})
            self.assertEqual(rejected.status_code, 404, rejected.json)
            unchanged = client.get('/neo4j_reusable_pipeline_version', query_string={'pipeline_uid': uid, 'version_uid': first})
            self.assertEqual(unchanged.json['graph'], pinned.json['graph'])

    @unittest.skipUnless(os.getenv('RUN_MINIO_INTEGRATION') == '1', 'requires disposable MinIO')
    def test_reusable_attachments_survive_source_removal_and_loading_autosave(self):
        from neo4j_api import _upload_bytes_to_minio
        from minio_access import remove_object, read_object_bytes
        from pathlib import Path
        graph = json.loads((Path(__file__).resolve().parents[2] / 'examples/order-summary/pipeline.json').read_text())
        source = graph['nodes'][0]['data']
        task = graph['nodes'][1]['data']
        source['files'] = [{'filename': 'orders.csv', 'bucket': 'files-step-id-orders', 'role': 'data'}]
        pointer = {'filename': 'main.py', 'bucket': 'files-step-id-summary', 'snapshot_bucket': 'files-step-id-summary', 'snapshot_object': '.generated/test/main.py', 'role': 'code'}
        # This is the old lossy catalog payload that caused the reported failure.
        task['files'] = ['main.py']
        task['generated_artifact'] = {'status': 'current', 'files': [pointer]}
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            _upload_bytes_to_minio('files-step-id-orders', 'orders.csv', b'order_id,amount\n1,2.50\n')
            _upload_bytes_to_minio('files-step-id-summary', '.generated/test/main.py', b"print('saved code')\n")
            client = app.test_client()
            saved = client.post('/neo4j_reusable_pipelines', json={'name': f'Files-{uuid.uuid4()}', 'graph': graph})
            self.assertEqual(saved.status_code, 200, saved.json)
            uid = saved.json['reference']['pipeline_uid']
            self.assertNotIn('version_uid', saved.json['reference'])
            remove_object('files-step-id-orders', 'orders.csv')
            remove_object('files-step-id-summary', '.generated/test/main.py')
            loaded = client.get('/neo4j_reusable_pipeline', query_string={'pipeline_uid': uid})
            self.assertEqual(loaded.status_code, 200, loaded.json)
            restored = loaded.json['graph']
            script = restored['nodes'][1]['data']['files'][0]
            self.assertEqual(read_object_bytes(script['snapshot_bucket'], script['snapshot_object']), b"print('saved code')\n")
            synced = client.post('/neo4j_sync_graph', json={'graph': restored})
            self.assertEqual(synced.status_code, 200, synced.json)
            autosaved = client.post('/neo4j_save_pipeline_active_version', json={'graph': restored})
            self.assertEqual(autosaved.status_code, 200, autosaved.json)
            remove_object(script['snapshot_bucket'], script['snapshot_object'])
            before = client.get('/neo4j_get_graph')
            rejected = client.post('/neo4j_reusable_pipelines', json={'name': f'Missing-{uuid.uuid4()}', 'graph': restored})
            self.assertEqual(rejected.status_code, 422, rejected.json)
            self.assertIn('attachment is unavailable', rejected.json['error'])
            self.assertEqual(client.get('/neo4j_get_graph').headers['ETag'], before.headers['ETag'])

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
