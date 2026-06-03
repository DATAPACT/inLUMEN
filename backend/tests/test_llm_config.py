import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_config import resolve_llm_config


class LLMConfigTest(unittest.TestCase):
    def test_requires_request_supplied_configuration(self):
        with self.assertRaisesRegex(ValueError, "LLM provider is required"):
            resolve_llm_config({})

    def test_ignores_backend_environment_llm_configuration(self):
        previous_values = {
            key: os.environ.get(key)
            for key in (
                "LLM_PROVIDER",
                "LLM_BASE_URL",
                "LLM_API_KEY",
                "LLM_MODEL",
            )
        }
        try:
            os.environ["LLM_PROVIDER"] = "openrouter"
            os.environ["LLM_BASE_URL"] = "https://openrouter.ai/api/v1"
            os.environ["LLM_API_KEY"] = "env-secret"
            os.environ["LLM_MODEL"] = "gpt-oss-120b"

            with self.assertRaisesRegex(ValueError, "LLM provider is required"):
                resolve_llm_config({})
        finally:
            for key, value in previous_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_requires_browser_supplied_api_key(self):
        with self.assertRaisesRegex(ValueError, "LLM API key is required"):
            resolve_llm_config({
                "provider": "openrouter",
                "model": "gpt-oss-120b",
                "base_url": "https://openrouter.ai/api/v1",
            })

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


if __name__ == "__main__":
    unittest.main()
