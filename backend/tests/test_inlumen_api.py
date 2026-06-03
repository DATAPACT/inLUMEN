import os
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inlumen_api import app  # noqa: E402


class InlumenApiTest(unittest.TestCase):
    def setUp(self):
        self.previous_values = {
            key: os.environ.get(key)
            for key in (
                "AUTH_ENABLED",
                "INLUMEN_DEV_LLM_CONFIG_ENABLED",
                "LLM_CONFIG_NAME",
                "LLM_PROVIDER",
                "LLM_BASE_URL",
                "LLM_API_KEY",
                "LLM_MODEL",
            )
        }
        os.environ["AUTH_ENABLED"] = "false"
        self.client = app.test_client()

    def tearDown(self):
        for key, value in self.previous_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_dev_llm_config_is_exposed_without_secret(self):
        os.environ["INLUMEN_DEV_LLM_CONFIG_ENABLED"] = "true"
        os.environ["LLM_CONFIG_NAME"] = "Dev OpenRouter"
        os.environ["LLM_PROVIDER"] = "openrouter"
        os.environ["LLM_BASE_URL"] = "https://openrouter.ai/api/v1"
        os.environ["LLM_API_KEY"] = "env-secret"
        os.environ["LLM_MODEL"] = "gpt-oss-120b"

        response = self.client.get("/api/chatbot-configs")

        self.assertEqual(200, response.status_code)
        config = response.get_json()["configs"][0]
        self.assertEqual("dev-env", config["id"])
        self.assertEqual("Dev OpenRouter", config["name"])
        self.assertTrue(config["serverManagedKey"])
        self.assertTrue(config["readOnly"])
        self.assertNotIn("env-secret", response.get_data(as_text=True))
        self.assertNotIn("apiKey", config)
        self.assertNotIn("api_key", config)

    def test_dev_llm_config_cannot_be_modified(self):
        os.environ["INLUMEN_DEV_LLM_CONFIG_ENABLED"] = "true"
        os.environ["LLM_CONFIG_NAME"] = "Dev OpenRouter"
        os.environ["LLM_PROVIDER"] = "openrouter"
        os.environ["LLM_BASE_URL"] = "https://openrouter.ai/api/v1"
        os.environ["LLM_API_KEY"] = "env-secret"
        os.environ["LLM_MODEL"] = "gpt-oss-120b"

        response = self.client.delete("/api/chatbot-configs/dev-env")

        self.assertEqual(403, response.status_code)


if __name__ == "__main__":
    unittest.main()
