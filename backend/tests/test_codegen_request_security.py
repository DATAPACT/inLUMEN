import json
import os
import unittest
from unittest.mock import patch

import inlumen_api


class CodegenRequestSecurityTests(unittest.TestCase):
    def test_codegen_separates_service_auth_from_provider_credentials(self):
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
            encoded, headers = inlumen_api._codegen_request_parts(payload)

        self.assertEqual("Bearer service-secret", headers["Authorization"])
        self.assertEqual("provider-secret", headers["X-LLM-API-Key"])
        self.assertNotIn("provider-secret", encoded.decode("utf-8"))
        outbound_config = json.loads(encoded)["llm_config"]
        self.assertEqual("test-model", outbound_config["model"])
        self.assertNotIn("api_key", outbound_config)

    def test_poll_request_only_sends_service_authentication(self):
        with patch.dict(
            os.environ,
            {"INLUMEN_CODEGEN_SERVICE_API_KEY": "service-secret"},
        ):
            encoded, headers = inlumen_api._codegen_request_parts()

        self.assertIsNone(encoded)
        self.assertEqual("Bearer service-secret", headers["Authorization"])
        self.assertNotIn("X-LLM-API-Key", headers)

    def test_resume_payload_uses_ephemeral_llm_transport(self):
        payload = {
            "flow_id": "clean",
            "repair_attempts": 4,
            "llm_config": {
                "provider": "openrouter",
                "model": "code-model",
                "base_url": "https://llm.example/v1",
                "api_key": "provider-secret",
            },
        }

        encoded, headers = inlumen_api._codegen_request_parts(payload)

        self.assertEqual("provider-secret", headers["X-LLM-API-Key"])
        self.assertEqual("clean", json.loads(encoded)["flow_id"])
        self.assertNotIn("api_key", json.loads(encoded)["llm_config"])

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

if __name__ == "__main__":
    unittest.main()
