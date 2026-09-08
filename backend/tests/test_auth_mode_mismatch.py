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


class AuthModeContinuityTests(unittest.TestCase):
    def check_transition(self, environment, previous, enabled, *, override=False):
        from unittest.mock import MagicMock
        from workspace_store import validate_auth_mode_continuity

        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (previous,) if previous else None
        env = {'DATABASE_URL': 'postgresql://test',
               'INLUMEN_ALLOW_AUTH_MODE_SWITCH': str(override).lower()}
        if environment is not None:
            env['APP_ENV'] = environment
        with patch.dict('os.environ', env, clear=True), \
             patch('workspace_store.ensure_schema'), \
             patch('workspace_store._connect') as connect:
            connect.return_value.__enter__.return_value = connection
            validate_auth_mode_continuity(enabled)
        return cursor

    def test_development_switches_both_directions_without_override(self):
        for environment in ('development', None):
            for previous, enabled, expected in (
                ('keycloak', False, 'local'), ('local', True, 'keycloak')
            ):
                with self.subTest(environment=environment, previous=previous):
                    cursor = self.check_transition(environment, previous, enabled)
                    self.assertEqual(cursor.execute.call_count, 2)
                    query, parameters = cursor.execute.call_args.args
                    self.assertIn('UPDATE application_runtime_settings', query)
                    self.assertEqual(parameters, (expected,))

    def test_other_environments_keep_transition_guard(self):
        from workspace_store import AuthModeTransitionError
        for environment in ('production', 'staging', ''):
            for previous, enabled in (('keycloak', False), ('local', True)):
                with self.subTest(environment=environment, previous=previous):
                    with self.assertRaises(AuthModeTransitionError):
                        self.check_transition(environment, previous, enabled)

    def test_explicit_migration_override_still_supported(self):
        cursor = self.check_transition('production', 'local', True, override=True)
        self.assertEqual(cursor.execute.call_args.args[1], ('keycloak',))

    def test_unchanged_mode_does_not_write(self):
        cursor = self.check_transition('production', 'keycloak', True)
        self.assertEqual(cursor.execute.call_count, 1)

    def test_first_start_records_mode(self):
        cursor = self.check_transition('development', None, False)
        self.assertIn('INSERT INTO application_runtime_settings', cursor.execute.call_args.args[0])
        self.assertEqual(cursor.execute.call_args.args[1], ('local',))

    def test_production_rejects_local_even_with_override(self):
        with patch.dict('os.environ', {'APP_ENV': 'production', 'AUTH_ENABLED': 'false',
                                      'INLUMEN_ALLOW_AUTH_MODE_SWITCH': 'true'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'AUTH_ENABLED must be true'):
                validate_production_auth_configuration()
