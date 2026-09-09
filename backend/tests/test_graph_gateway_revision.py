import json
import unittest
from unittest.mock import patch

import inlumen_api
from local_api_client import LocalApiResponse


class GraphGatewayRevisionTests(unittest.TestCase):
    def test_delete_responses_preserve_revision_after_storage_cleanup(self):
        graph = LocalApiResponse(content=json.dumps({'deleted_step_flow_ids': ['node-1']}).encode(),
                                 status_code=200, headers={'ETag': '"12"'})
        storage = LocalApiResponse(content=b'{}', status_code=200, headers={'ETag': '"object-etag"'})
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}), \
             patch.object(inlumen_api, 'dispatch_graph_request', return_value=graph), \
             patch.object(inlumen_api, 'dispatch_object_request', return_value=storage), \
             patch.object(inlumen_api, 'clear_node_secrets'):
            client = inlumen_api.app.test_client()
            for path in ['/api/graph/nodes', '/api/graph/nodes/node-1']:
                with self.subTest(path=path):
                    response = client.delete(path, headers={'If-Match': '"11"'})
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(response.headers['ETag'], '"12"')
                    self.assertIn('storage_cleanup', response.json)

    def test_sequential_graph_writes_use_only_the_last_successful_write_revision(self):
        def response(revision, status=200):
            return LocalApiResponse(content=b'{}', status_code=status, headers={'ETag': f'"{revision}"'})

        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}), \
             patch.object(inlumen_api, 'dispatch_graph_request', side_effect=[
                 response(11), response(12), response(14), response(14, 409), response(14, 409),
             ]) as dispatch:
            with inlumen_api.app.test_request_context('/api/nodes/node-1/files', method='POST', headers={'If-Match': '"11"'}):
                # File validation reads, attachment write, artifact read and update.
                for method in ['GET', 'POST', 'GET', 'POST', 'POST']:
                    inlumen_api._proxy(inlumen_api.dispatch_graph_request, 'test', method=method)
            self.assertEqual([call.kwargs['headers']['If-Match'] for call in dispatch.call_args_list],
                             ['"11"', '"11"', '"12"', '"12"', '"12"'])

    def test_source_upload_returns_graph_revision_not_object_storage_etag(self):
        import io
        graph = {'nodes': [{'id': 'node-1', 'data': {'type': 'source'}}], 'edges': []}
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}), \
             patch.object(inlumen_api, 'dispatch_graph_request', side_effect=[
                 LocalApiResponse(content=json.dumps(graph).encode(), status_code=200, headers={'ETag': '"11"'}),
                 LocalApiResponse(content=b'{}', status_code=200, headers={'ETag': '"12"'}),
             ]), \
             patch.object(inlumen_api, 'dispatch_object_request', return_value=LocalApiResponse(
                 content=b'{}', status_code=200, headers={'ETag': '"object-etag"'})):
            response = inlumen_api.app.test_client().post('/api/nodes/node-1/files',
                headers={'If-Match': '"11"'}, data={'file': (io.BytesIO(b'name,value\nexample,1\n'), 'input.csv'), 'role': 'data'})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.headers['ETag'], '"12"')

    def test_stale_file_mutations_are_rejected_before_object_storage_changes(self):
        graph = LocalApiResponse(content=b'{"nodes": [], "edges": []}', status_code=200, headers={'ETag': '"12"'})
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}), \
             patch.object(inlumen_api, 'dispatch_graph_request', return_value=graph), \
             patch.object(inlumen_api, 'dispatch_object_request') as storage:
            client = inlumen_api.app.test_client()
            for method, path in [('POST', '/api/nodes/1/files'), ('DELETE', '/api/nodes/1/files'), ('PUT', '/api/nodes/1/files/text')]:
                with self.subTest(method=method):
                    response = client.open(path, method=method, headers={'If-Match': '"11"'})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json['code'], 'graph_conflict')
                    self.assertEqual(response.headers['ETag'], '"12"')
            storage.assert_not_called()
