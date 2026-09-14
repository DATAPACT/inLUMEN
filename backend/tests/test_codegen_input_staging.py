import base64
import hashlib
import unittest
from unittest.mock import patch
from flask import g

import inlumen_api as api
from local_api_client import LocalApiResponse
from workspace_store import Principal
from workspace_storage import node_bucket_name, version_snapshot_bucket


class InputStagingTests(unittest.TestCase):
    def setUp(self):
        self.auth = patch.dict('os.environ', {'AUTH_ENABLED': 'true', 'APP_ENV': 'production'})
        self.auth.start()
        self.context = api.app.test_request_context()
        self.context.push()
        g.inlumen_principal = Principal('u', 'u', 'issuer', 'workspace-a', 'tenant', 'owner')
        self.ref = {'filename': 'input.csv', 'bucket': node_bucket_name('1'),
                    'snapshot_bucket': version_snapshot_bucket(), 'snapshot_object': 'immutable/input.csv'}
        self.payload = {'context': {'graph': {'nodes': [{'files': [self.ref]}]}}}

    def tearDown(self):
        self.context.pop()
        self.auth.stop()

    @patch('inlumen_api._proxy')
    def test_exact_snapshot_bytes_transferred_without_a_credential(self, proxy):
        content = b'value\n' + b'real-row\n' * 3000
        proxy.return_value = LocalApiResponse(content, 200, {})
        result = api._prepare_codegen_request(self.payload)
        sample = result['context']['graph']['nodes'][0]['files'][0]['sample']
        self.assertEqual(base64.b64decode(sample['content_base64']), content)
        self.assertEqual(sample['content_sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(proxy.call_args.kwargs['params']['bucket_name'], version_snapshot_bucket())
        self.assertEqual(proxy.call_args.kwargs['params']['filename'], 'immutable/input.csv')
        self.assertNotIn('sample', self.ref)  # input metadata remains unchanged

    @patch('inlumen_api._proxy')
    def test_cross_workspace_snapshot_rejected_before_read(self, proxy):
        self.ref['snapshot_bucket'] = version_snapshot_bucket('workspace-b')
        with self.assertRaisesRegex(ValueError, 'current workspace'):
            api._prepare_codegen_request(self.payload)
        proxy.assert_not_called()

    @patch('inlumen_api._proxy')
    def test_missing_input_fails_instead_of_substituting_preview(self, proxy):
        proxy.return_value = LocalApiResponse(b'', 404, {})
        with self.assertRaisesRegex(ValueError, 'could not be read'):
            api._prepare_codegen_request(self.payload)

    @patch('inlumen_api._proxy')
    def test_transport_limits_and_user_metadata_are_not_traversed(self, proxy):
        proxy.return_value = LocalApiResponse(b'12345', 200, {})
        with patch.dict('os.environ', {'INLUMEN_CODEGEN_INPUT_MAX_BYTES': '4'}):
            with self.assertRaisesRegex(ValueError, 'transport limit'):
                api._prepare_codegen_request(self.payload)
        proxy.reset_mock()
        api._prepare_codegen_request({'context': {'graph': {'nodes': [{'parameters': self.ref}]}}})
        proxy.assert_not_called()

    @patch('inlumen_api._proxy')
    def test_unauthenticated_development_keeps_optional_download_behavior(self, proxy):
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false', 'APP_ENV': 'development'}):
            result = api._prepare_codegen_request(self.payload)
        self.assertNotIn('sample', result['context']['graph']['nodes'][0]['files'][0])
        proxy.assert_not_called()
