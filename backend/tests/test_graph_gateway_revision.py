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
