import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_config import server_managed_llm_config_metadata, resolve_llm_config


class LLMConfigTest(unittest.TestCase):
    def test_requires_request_supplied_configuration(self):
        with self.assertRaisesRegex(ValueError, "LLM provider is required"):
            resolve_llm_config({})

    def test_ignores_backend_environment_fallbacks_unless_dev_mode_enabled(self):
        previous_values = {
            key: os.environ.get(key)
            for key in (
                "INLUMEN_DEV_LLM_CONFIG_ENABLED",
                "LLM_PROVIDER",
                "LLM_BASE_URL",
                "LLM_API_KEY",
                "LLM_MODEL",
            )
        }
        try:
            os.environ["INLUMEN_DEV_LLM_CONFIG_ENABLED"] = "false"
            os.environ["LLM_PROVIDER"] = "openrouter"
            os.environ["LLM_BASE_URL"] = "https://openrouter.ai/api/v1"
            os.environ["LLM_API_KEY"] = "env-secret"
            os.environ["LLM_MODEL"] = "gpt-oss-120b"

            with self.assertRaisesRegex(ValueError, "LLM provider is required"):
                resolve_llm_config({})

            with self.assertRaisesRegex(ValueError, "LLM API key is required"):
                resolve_llm_config({
                    "provider": "openrouter",
                    "model": "gpt-oss-120b",
                    "base_url": "https://openrouter.ai/api/v1",
                })
        finally:
            for key, value in previous_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_accepts_browser_supplied_configuration(self):
        config = resolve_llm_config({
            "provider": "openrouter",
            "model": "gpt-oss-120b",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "session-secret",
        })

        self.assertEqual("openrouter", config.provider)
        self.assertEqual("openai/gpt-oss-120b", config.model)
        self.assertEqual("https://openrouter.ai/api/v1", config.base_url)
        self.assertEqual("session-secret", config.api_key)

    def test_accepts_server_managed_dev_configuration_when_enabled(self):
        previous_values = {
            key: os.environ.get(key)
            for key in (
                "INLUMEN_DEV_LLM_CONFIG_ENABLED",
                "LLM_CONFIG_NAME",
                "LLM_PROVIDER",
                "LLM_BASE_URL",
                "LLM_API_KEY",
                "LLM_MODEL",
            )
        }
        try:
            os.environ["INLUMEN_DEV_LLM_CONFIG_ENABLED"] = "true"
            os.environ["LLM_CONFIG_NAME"] = "Dev OpenRouter"
            os.environ["LLM_PROVIDER"] = "openrouter"
            os.environ["LLM_BASE_URL"] = "https://openrouter.ai/api/v1"
            os.environ["LLM_API_KEY"] = "env-secret"
            os.environ["LLM_MODEL"] = "gpt-oss-120b"

            metadata = server_managed_llm_config_metadata()
            self.assertIsNotNone(metadata)
            self.assertEqual("Dev OpenRouter", metadata["name"])
            self.assertTrue(metadata["serverManagedKey"])
            self.assertNotIn("api_key", metadata)
            self.assertNotIn("apiKey", metadata)

            config = resolve_llm_config({"serverManagedKey": True})
            self.assertEqual("openrouter", config.provider)
            self.assertEqual("openai/gpt-oss-120b", config.model)
            self.assertEqual("https://openrouter.ai/api/v1", config.base_url)
            self.assertEqual("env-secret", config.api_key)
        finally:
            for key, value in previous_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
