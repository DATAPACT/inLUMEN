import json
import os
import unittest
from unittest.mock import patch

import inlumen_api


class CodegenRequestSecurityTests(unittest.TestCase):
    def test_codegen_headers_are_added_and_llm_key_is_removed_from_json(self):
        payload = {
            "context": {"graph": {"nodes": [], "edges": []}},
            "llm_config": {
                "model": "test-model",
                "api_key": "provider-secret",
            },
        }

        with patch.dict(
            os.environ,
            {"INLUMEN_CODEGEN_SERVICE_API_KEY": "service-secret"},
        ):
            encoded, headers = inlumen_api._codegen_request_parts(
                payload,
                include_llm_key=True,
            )

        self.assertEqual("Bearer service-secret", headers["Authorization"])
        self.assertEqual("provider-secret", headers["X-LLM-API-Key"])
        self.assertNotIn("provider-secret", encoded.decode("utf-8"))
        self.assertEqual(
            {"model": "test-model"},
            json.loads(encoded)["llm_config"],
        )

    def test_poll_request_only_sends_service_authentication(self):
        with patch.dict(
            os.environ,
            {"INLUMEN_CODEGEN_SERVICE_API_KEY": "service-secret"},
        ):
            encoded, headers = inlumen_api._codegen_request_parts()

        self.assertIsNone(encoded)
        self.assertEqual("Bearer service-secret", headers["Authorization"])
        self.assertNotIn("X-LLM-API-Key", headers)

    def test_resume_can_forward_a_fresh_llm_key_without_adding_it_to_json(self):
        payload = {
            "flow_id": "clean",
            "repair_attempts": 4,
        }

        encoded, headers = inlumen_api._codegen_request_parts(
            payload,
            include_llm_key=True,
            llm_api_key="fresh-provider-secret",
        )

        self.assertEqual(
            "fresh-provider-secret",
            headers["X-LLM-API-Key"],
        )
        self.assertNotIn("fresh-provider-secret", encoded.decode("utf-8"))

    def test_pipeline_codegen_payload_maps_single_pass_to_pipeline_first(self):
        codegen_payload, metadata = inlumen_api._build_pipeline_codegen_payload(
            {"nodes": [], "edges": []},
            {
                "generation_mode": "full",
                "generation_strategy": "single_pass",
            },
        )

        self.assertEqual(
            "pipeline_first",
            codegen_payload["options"]["generation_strategy"],
        )
        self.assertEqual(
            "pipeline_first",
            metadata["options"]["generation_strategy"],
        )

    def test_pipeline_codegen_payload_maps_per_node_to_node_first(self):
        codegen_payload, metadata = inlumen_api._build_pipeline_codegen_payload(
            {"nodes": [], "edges": []},
            {"generation_strategy": "per_node"},
        )

        self.assertEqual(
            "node_first",
            codegen_payload["options"]["generation_strategy"],
        )
        self.assertEqual(
            "node_first",
            metadata["options"]["generation_strategy"],
        )

    def test_codegen_llm_config_uses_the_settings_payload(self):
        config = inlumen_api._codegen_llm_config_from_payload(
            {
                "llm_config": {
                    "model": "coding-model",
                    "base_url": "https://provider.example/v1",
                    "api_key": "same-provider-token",
                    "timeout_seconds": 90,
                },
            },
        )

        self.assertEqual(
            {
                "model": "coding-model",
                "base_url": "https://provider.example/v1",
                "api_key": "same-provider-token",
                "timeout_seconds": 90,
            },
            config,
        )


if __name__ == "__main__":
    unittest.main()
