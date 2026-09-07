import unittest
from unittest.mock import patch
from flask import Flask, jsonify
from auth_middleware import is_auth_enabled, require_auth, validate_production_auth_configuration


class AuthModeMismatchTests(unittest.TestCase):
    def test_typo_never_silently_disables_authentication(self):
        for value in ('trye', '', '1', 'yes'):
            with self.subTest(value=value), patch.dict('os.environ', {'AUTH_ENABLED': value, 'APP_ENV': 'development'}):
                with self.assertRaisesRegex(RuntimeError, 'AUTH_ENABLED must be exactly'):
                    is_auth_enabled()
                with self.assertRaises(RuntimeError):
                    validate_production_auth_configuration()

    def test_local_mode_rejects_authenticated_reads_and_writes(self):
        app = Flask(__name__)
        called = []
        @app.route('/test', methods=['GET', 'POST'])
        @require_auth
        def endpoint():
            called.append(True)
            return jsonify(ok=True)
        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            client = app.test_client()
            for method in ('GET', 'POST'):
                response = client.open('/test', method=method, headers={'Authorization': 'Bearer stale-private-token'})
                self.assertEqual(response.status_code, 409)
                self.assertIn('configuration mismatch', response.json['error'])
            self.assertEqual(called, [])
            self.assertEqual(client.get('/test').status_code, 200)
