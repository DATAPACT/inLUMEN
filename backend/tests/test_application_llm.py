import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import g

import inlumen_api
from application_llm import APPLICATION_LLM_ID, application_llm_request_config, load_application_llm, validate_application_llm_config, application_llm_settings
import application_llm_store
from application_llm_store import save_application_llm, read_application_llm
from auth_middleware import AuthValidationError, current_workspace_id
from llm_config import resolve_llm_config
from workspace_store import Principal


def principal(name="alice"):
    return Principal(
        user_id=name, subject=name, issuer="https://identity.test/realms/test",
        workspace_id=f"workspace-{name}", tenant_id=f"tenant-{name}",
        workspace_role="owner", display_name=name,
    )


class ApplicationLLMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "APP_ENV": "development", "AUTH_ENABLED": "true", "DATABASE_URL": "",
            "INLUMEN_SECRET_DB_PATH": str(Path(self.temp.name) / "secrets.sqlite3"),
            "INLUMEN_SECRET_KEY_PATH": str(Path(self.temp.name) / "secrets.key"),
            "INLUMEN_SECRET_ENCRYPTION_KEY": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = inlumen_api.app.test_client()
        self.config = validate_application_llm_config({"model": "test/chat-model", "codegenModel": "test/code-model"})
        save_application_llm(self.config, "shared-secret", enabled=True, revision=0, updated_by="admin")

    def test_disabled_does_not_decrypt_and_rejects_stale_selection(self):
        save_application_llm(self.config, "", enabled=False, revision=1, updated_by="admin")
        with patch("application_llm_store._fernet") as cipher:
            self.assertIsNone(load_application_llm())
            self.assertIsNotNone(application_llm_settings()["config"])
            cipher.assert_not_called()
        with inlumen_api.app.test_request_context():
            g.inlumen_principal = principal()
            with self.assertRaisesRegex(ValueError, "disabled"):
                resolve_llm_config({"credential_id": APPLICATION_LLM_ID})

    def test_provider_is_inferred_from_url_and_codegen_defaults_to_chat(self):
        for url, provider in (("https://ollama.com/v1/", "ollama_cloud"), ("https://llm.test/v1", "custom")):
            with self.subTest(url=url):
                config = validate_application_llm_config({"model": "test/model", "baseUrl": url})
                self.assertEqual(config["provider"], provider)
                self.assertEqual(config["codegen_model"], "test/model")

    def test_requires_an_authorized_request(self):
        for raw in ({"credential_id": APPLICATION_LLM_ID}, {"config_id": APPLICATION_LLM_ID}):
            with self.assertRaisesRegex(ValueError, "authorized workspace"):
                resolve_llm_config(raw)
            with inlumen_api.app.test_request_context():
                with self.assertRaisesRegex(ValueError, "authorized workspace"):
                    resolve_llm_config(raw)

    def test_rejects_unauthenticated_production_even_with_local_principal(self):
        with patch.dict(os.environ, {"APP_ENV": "production", "AUTH_ENABLED": "false"}), \
             inlumen_api.app.test_request_context():
            g.inlumen_principal = principal()
            with self.assertRaisesRegex(ValueError, "authentication in production"):
                resolve_llm_config({"credential_id": APPLICATION_LLM_ID})

    def test_chat_replaces_all_client_connection_settings(self):
        malicious = {
            "credential_id": APPLICATION_LLM_ID, "provider": "custom",
            "base_url": "https://attacker.test/v1", "baseUrl": "https://attacker.test",
            "model": "expensive-model", "api_key": "attacker-key", "apiKey": "other-key",
            "max_tokens": 99999999, "openrouter_provider_only": ["unapproved"],
        }
        with inlumen_api.app.test_request_context():
            g.inlumen_principal = principal()
            resolved = resolve_llm_config(malicious)
        self.assertEqual(resolved.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(resolved.provider, "openrouter")
        self.assertEqual(resolved.api_key, "shared-secret")
        self.assertEqual(resolved.model, "test/chat-model")
        self.assertEqual(resolved.max_tokens, 8192)
        self.assertEqual(resolved.openrouter_provider_only, ())
        self.assertNotIn("shared-secret", repr(resolved))
        self.assertEqual(malicious["api_key"], "attacker-key")

    def test_codegen_uses_trusted_model_and_header_without_mutating_payload(self):
        payload = {"llm_config": {
            "config_id": APPLICATION_LLM_ID, "provider": "custom",
            "model": "expensive-model", "base_url": "https://attacker.test",
            "max_output_tokens": 32768, "openrouter_provider_only": ["unapproved"],
            "apiKey": "attacker-key",
        }}
        original = json.dumps(payload)
        with inlumen_api.app.test_request_context():
            g.inlumen_principal = principal()
            encoded, headers = inlumen_api._codegen_request_parts(payload)
        config = json.loads(encoded)["llm_config"]
        self.assertEqual(config["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(config["model"], "test/code-model")
        self.assertEqual(config["max_output_tokens"], 8192)
        self.assertEqual(config["openrouter_provider_only"], [])
        self.assertEqual(headers["X-LLM-API-Key"], "shared-secret")
        self.assertEqual(headers["X-InLumen-Workspace-Id"], "workspace-alice")
        for forbidden in ("shared-secret", "attacker", APPLICATION_LLM_ID, "api_key", "apiKey"):
            self.assertNotIn(forbidden, encoded.decode())
        self.assertEqual(json.dumps(payload), original)

    def test_personal_credentials_do_not_fall_back_to_shared_key(self):
        raw = {"provider": "custom", "model": "personal", "base_url": "https://private.test", "credential_id": "missing"}
        self.assertIs(application_llm_request_config(raw), raw)
        with patch("llm_credential_store.get_llm_credential", return_value=None):
            with self.assertRaisesRegex(ValueError, "API key is required"):
                resolve_llm_config(raw)

    def test_invalid_config_fails_without_exposing_secret(self):
        for url in ("http://insecure.test", "https://user:pass@test", "https://test?api_key=shared-secret", "https://[invalid"):
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                validate_application_llm_config({"model": "test/model", "baseUrl": url})
            self.assertNotIn("shared-secret", str(error.exception))
        with self.assertRaisesRegex(ValueError, "model is required"):
            validate_application_llm_config({})

    def test_encrypted_storage_rotation_and_metadata(self):
        self.assertNotIn("shared-secret", Path(self.temp.name, "secrets.sqlite3").read_bytes().decode("latin-1"))
        with patch("application_llm_store._fernet") as cipher:
            self.assertNotIn("shared-secret", json.dumps(application_llm_settings()))
            cipher.assert_not_called()
        save_application_llm(self.config, "replacement", enabled=True, revision=1, updated_by="admin")
        self.assertEqual(load_application_llm().api_key, "replacement")
        self.assertNotIn("replacement", repr(load_application_llm()))
        application_llm_store._ENGINE.dispose()
        application_llm_store._ENGINE = None
        self.assertEqual(load_application_llm().api_key, "replacement")

    def test_config_api_requires_keycloak_authentication(self):
        with patch("auth_middleware.validate_keycloak_bearer_token", return_value=(None, AuthValidationError(401, "Unauthorized", "Missing Bearer token"))):
            for path in ("/api/chatbot-configs", f"/api/chatbot-configs/{APPLICATION_LLM_ID}"):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_available_in_two_workspaces_without_copying_or_exposing_key(self):
        with patch("auth_middleware.validate_keycloak_bearer_token", return_value=({"sub": "user"}, None)), \
             patch("auth_middleware.resolve_principal") as resolve, \
             patch.object(inlumen_api, "_load_chatbot_configs", side_effect=lambda: [{"id": current_workspace_id(), "name": "Personal"}]), \
             patch.object(inlumen_api, "has_llm_credential", return_value=False), \
             patch.object(inlumen_api, "_save_chatbot_configs") as save:
            for name in ("alice", "bob"):
                resolve.return_value = principal(name)
                response = self.client.get("/api/chatbot-configs", headers={"Authorization": f"Bearer {name}"})
                self.assertEqual(response.status_code, 200)
                configs = response.json["configs"]
                self.assertEqual([c["id"] for c in configs], [APPLICATION_LLM_ID, f"workspace-{name}"])
                self.assertTrue(configs[0]["readOnly"])
                self.assertTrue(configs[0]["has_api_key"])
                self.assertNotIn("shared-secret", response.text)
                single = self.client.get(f"/api/chatbot-configs/{APPLICATION_LLM_ID}")
                self.assertEqual(single.json["config"], configs[0])
            save.assert_not_called()

    def test_reserved_config_cannot_be_created_changed_or_deleted(self):
        with patch("auth_middleware.validate_keycloak_bearer_token", return_value=({}, None)), \
             patch("auth_middleware.resolve_principal", return_value=principal()), \
             patch.object(inlumen_api, "_load_chatbot_configs", return_value=[]), \
             patch.object(inlumen_api, "_save_chatbot_configs") as save, \
             patch.object(inlumen_api, "save_llm_credential") as save_key, \
             patch.object(inlumen_api, "delete_llm_credential") as delete_key:
            for method, path in (("POST", "/api/chatbot-configs"), ("PUT", f"/api/chatbot-configs/{APPLICATION_LLM_ID}"), ("DELETE", f"/api/chatbot-configs/{APPLICATION_LLM_ID}")):
                response = self.client.open(path, method=method, json={"id": APPLICATION_LLM_ID, "api_key": "replacement"})
                self.assertEqual(response.status_code, 403)
            save.assert_not_called()
            save_key.assert_not_called()
            delete_key.assert_not_called()

    def test_disabled_config_disappears_from_api(self):
        save_application_llm(self.config, "", enabled=False, revision=1, updated_by="admin")
        with patch("auth_middleware.validate_keycloak_bearer_token", return_value=({}, None)), \
             patch("auth_middleware.resolve_principal", return_value=principal()), \
             patch.object(inlumen_api, "_load_chatbot_configs", return_value=[]):
            self.assertEqual(self.client.get("/api/chatbot-configs").json, {"configs": []})
            self.assertEqual(self.client.get(f"/api/chatbot-configs/{APPLICATION_LLM_ID}").status_code, 404)
