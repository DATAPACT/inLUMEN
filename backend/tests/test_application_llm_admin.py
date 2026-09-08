import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import inlumen_api
import application_llm_store
from application_llm import load_application_llm
from application_llm_store import read_application_llm
from auth_middleware import AuthValidationError
from tests.test_application_llm import principal

ADMIN_URL = "/api/admin/application-llm"
ADMIN_CLAIMS = {"realm_access": {"roles": ["inlumen-admin"]}}


class ApplicationLLMAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for context in (
            patch.dict(os.environ, {"APP_ENV": "development", "AUTH_ENABLED": "true", "DATABASE_URL": "",
                "INLUMEN_SECRET_DB_PATH": str(Path(self.temp.name) / "secrets.sqlite3"),
                "INLUMEN_SECRET_KEY_PATH": str(Path(self.temp.name) / "key"), "INLUMEN_SECRET_ENCRYPTION_KEY": ""}),
        ):
            context.start()
            self.addCleanup(context.stop)
        auth = patch("auth_middleware.validate_keycloak_bearer_token", return_value=(ADMIN_CLAIMS, None))
        self.auth = auth.start()
        self.addCleanup(auth.stop)
        identity = patch("auth_middleware.resolve_principal", return_value=principal("admin"))
        self.identity = identity.start()
        self.addCleanup(identity.stop)
        self.client = inlumen_api.app.test_client()

    def save(self, *, revision=0, enabled=True, key="provider-secret", **config):
        return self.client.put(ADMIN_URL, json={
            "config": {"model": "test/chat", "api_key": key, **config},
            "enabled": enabled, "revision": revision,
        })

    def test_admin_can_save_disable_enable_and_rotate_without_exposing_key(self):
        self.assertEqual(self.client.get(ADMIN_URL).json, {"config": None, "enabled": False, "revision": 0})
        saved = self.save()
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertTrue(saved.json["enabled"])
        self.assertTrue(saved.json["config"]["has_api_key"])
        self.assertNotIn("provider-secret", saved.text)
        self.assertEqual(load_application_llm().api_key, "provider-secret")
        self.assertEqual(self.save(revision=1, enabled=False, key="").status_code, 200)
        self.assertIsNone(load_application_llm())
        self.assertTrue(self.client.get(ADMIN_URL).json["config"]["has_api_key"])
        self.assertEqual(self.save(revision=2, key="").status_code, 200)
        self.assertEqual(load_application_llm().api_key, "provider-secret")
        self.assertEqual(self.save(revision=3, key="rotated-key").status_code, 200)
        self.assertEqual(load_application_llm().api_key, "rotated-key")
        self.assertNotIn("rotated-key", self.client.get(ADMIN_URL).text)
        self.assertNotIn("rotated-key", Path(self.temp.name, "secrets.sqlite3").read_bytes().decode("latin-1"))

    def test_workspace_owners_and_forged_client_claims_cannot_administer(self):
        invalid_claims = [
            {}, {"preferred_username": "admin", "is_application_admin": True},
            {"realm_access": {"roles": "inlumen-admin"}},
            {"realm_access": ["inlumen-admin"]},
            {"resource_access": {"other-client": {"roles": ["inlumen-admin"]}}},
        ]
        for claims in invalid_claims:
            self.auth.return_value = (claims, None)
            for method in ("GET", "PUT"):
                with self.subTest(claims=claims, method=method), patch.object(inlumen_api, "save_application_llm") as write:
                    response = self.client.open(ADMIN_URL, method=method,
                        headers={"X-Application-Admin": "true"}, json={"is_application_admin": True})
                    self.assertEqual(response.status_code, 403)
                    write.assert_not_called()

    def test_invalid_token_cannot_grant_admin_even_with_role_claim(self):
        self.auth.return_value = (ADMIN_CLAIMS, AuthValidationError(401, "Unauthorized", "Invalid signature"))
        self.assertEqual(self.save().status_code, 401)

    def test_admin_role_is_independent_of_workspace_edit_role(self):
        self.identity.return_value = replace(principal("admin"), workspace_role="viewer")
        self.assertEqual(self.save().status_code, 200)
        # It does not grant arbitrary edit access to other workspace endpoints.
        self.assertEqual(self.client.post("/api/chatbot-configs", json={}).status_code, 403)

    def test_role_removal_is_enforced_on_the_next_request(self):
        self.assertEqual(self.save().status_code, 200)
        self.auth.return_value = ({}, None)
        self.assertEqual(self.save(revision=1, enabled=False).status_code, 403)
        self.assertTrue(read_application_llm()["enabled"])

    def test_stale_admin_cannot_overwrite_newer_key_or_enable_state(self):
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.save(revision=1, key="new-key", enabled=False).status_code, 200)
        stale = self.save(revision=1, enabled=True)
        self.assertEqual(stale.status_code, 409)
        self.assertFalse(read_application_llm()["enabled"])
        self.assertEqual(read_application_llm()["revision"], 2)

    def test_url_change_requires_a_replacement_key(self):
        self.assertEqual(self.save().status_code, 200)
        rejected = self.save(revision=1, key="", baseUrl="https://different.test/v1")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(load_application_llm().base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(self.save(revision=1, key="different-key", baseUrl="https://different.test/v1").status_code, 200)
        self.assertEqual(load_application_llm().api_key, "different-key")

    def test_rejects_invalid_settings_without_writing(self):
        for overrides in ({"revision": True}, {"enabled": "true"}, {"config": []}, {"config": {"model": "test", "api_key": "secret\nheader"}}):
            payload = {"revision": 0, "enabled": True, "config": {"model": "test", "api_key": "secret"}, **overrides}
            with self.subTest(overrides=overrides):
                self.assertEqual(self.client.put(ADMIN_URL, json=payload).status_code, 400)
        self.assertIsNone(read_application_llm()["config"])
        self.assertEqual(self.save(key="").status_code, 400)

    def test_session_reports_only_server_verified_admin_capability(self):
        with patch.object(inlumen_api, "list_workspaces", return_value=[]):
            self.assertTrue(self.client.get("/api/session").json["is_application_admin"])
            self.auth.return_value = ({}, None)
            self.assertFalse(self.client.get("/api/session").json["is_application_admin"])

    def test_local_admin_is_only_available_outside_production(self):
        with patch.dict(os.environ, {"AUTH_ENABLED": "false"}):
            self.assertEqual(self.client.get(ADMIN_URL).status_code, 200)
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                self.assertEqual(self.save().status_code, 403)
