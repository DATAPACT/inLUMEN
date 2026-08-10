import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import inlumen_api


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class CodegenServiceClientTest(unittest.TestCase):
    @staticmethod
    def headers(request):
        return {name.lower(): value for name, value in request.header_items()}

    @patch.dict(
        os.environ,
        {"INLUMEN_CODEGEN_SERVICE_API_KEY": "service-token"},
        clear=False,
    )
    @patch("inlumen_api.urlopen")
    def test_generation_uses_service_auth_and_ephemeral_llm_key(self, urlopen):
        urlopen.return_value = FakeResponse({"status": "ok"})
        payload = {
            "context": {},
            "llm_config": {
                "model": "test-model",
                "base_url": "https://llm.example/v1",
                "api_key": "provider-token",
            },
        }

        response = inlumen_api._post_codegen_request(payload)

        self.assertEqual({"status": "ok"}, response)
        outbound_request = urlopen.call_args.args[0]
        headers = self.headers(outbound_request)
        self.assertEqual("Bearer service-token", headers["authorization"])
        self.assertEqual("provider-token", headers["x-llm-api-key"])

        outbound_payload = json.loads(outbound_request.data.decode("utf-8"))
        self.assertEqual("test-model", outbound_payload["llm_config"]["model"])
        self.assertNotIn("api_key", outbound_payload["llm_config"])
        self.assertEqual("provider-token", payload["llm_config"]["api_key"])

    @patch.dict(
        os.environ,
        {"INLUMEN_CODEGEN_SERVICE_API_KEY": "service-token"},
        clear=False,
    )
    @patch("inlumen_api.urlopen")
    def test_resume_sends_sanitized_llm_configuration(self, urlopen):
        urlopen.return_value = FakeResponse({"run_id": "resumed"})
        payload = {
            "flow_id": "node-2",
            "repair_attempts": 4,
            "llm_config": {
                "provider": "openrouter",
                "model": "test-model",
                "base_url": "https://llm.example/v1",
                "apiKey": "provider-token",
            },
        }

        inlumen_api._post_codegen_pipeline_run_resume_request("source", payload)

        outbound_request = urlopen.call_args.args[0]
        headers = self.headers(outbound_request)
        self.assertEqual("Bearer service-token", headers["authorization"])
        self.assertEqual("provider-token", headers["x-llm-api-key"])
        outbound_payload = json.loads(outbound_request.data.decode("utf-8"))
        self.assertEqual("test-model", outbound_payload["llm_config"]["model"])
        self.assertNotIn("apiKey", outbound_payload["llm_config"])

    @patch.dict(
        os.environ,
        {"INLUMEN_CODEGEN_SERVICE_API_KEY": "service-token"},
        clear=False,
    )
    @patch("inlumen_api.urlopen")
    def test_generation_run_poll_uses_service_auth(self, urlopen):
        urlopen.return_value = FakeResponse({"run_id": "run-1"})

        inlumen_api._get_codegen_pipeline_run_request("run-1")

        outbound_request = urlopen.call_args.args[0]
        headers = self.headers(outbound_request)
        self.assertEqual("Bearer service-token", headers["authorization"])
        self.assertNotIn("x-llm-api-key", headers)

    @patch.dict(
        os.environ,
        {"INLUMEN_CODEGEN_SERVICE_API_KEY": "service-token"},
        clear=False,
    )
    @patch("inlumen_api.urlopen")
    def test_generation_run_cancel_uses_delete_and_service_auth(self, urlopen):
        urlopen.return_value = FakeResponse(
            {"run_id": "run-1", "status": "cancelled"}
        )

        response = inlumen_api._cancel_codegen_pipeline_run_request("run-1")

        self.assertEqual("cancelled", response["status"])
        outbound_request = urlopen.call_args.args[0]
        self.assertEqual("DELETE", outbound_request.get_method())
        headers = self.headers(outbound_request)
        self.assertEqual("Bearer service-token", headers["authorization"])
        self.assertNotIn("x-llm-api-key", headers)

    def test_node_descriptor_carries_runtime_model_plan_to_codegen(self):
        model_plan = {
            "task": "automatic_speech_recognition",
            "domain": "customer conversation",
            "framework": "faster-whisper",
            "model_id": "large-v3",
            "model_revision": "pinned-revision",
            "required_packages": ["faster-whisper==1.2.1"],
            "inference_parameters": {"beam_size": 5},
        }
        node = {
            "id": "3",
            "data": {
                "label": "Speech-to-Text",
                "description": "Transcribe conversational audio.",
                "type": "action",
                "param": {"model_plan": model_plan},
            },
        }

        descriptor = inlumen_api._node_descriptor(node)

        self.assertEqual(
            "Systran/faster-whisper-large-v3",
            descriptor["implementation"]["model_id"],
        )
        self.assertEqual(
            "edaa852ec7e145841d8ffdb056a99866b5f0a478",
            descriptor["implementation"]["model_revision"],
        )
        self.assertEqual(
            "faster-whisper",
            descriptor["implementation"]["adapter_id"],
        )
        self.assertEqual(model_plan, descriptor["parameters"]["model_plan"])

    def test_dynamic_model_plan_participates_in_codegen_configuration_hash(self):
        model_plan = {
            "framework": "transformers",
            "model_id": "openai/whisper-large-v3",
            "model_revision": "revision-1",
            "inference_parameters": {"cpu_threads": 2},
        }
        graph = {
            "nodes": [
                {
                    "id": "3",
                    "data": {
                        "label": "Speech-to-Text",
                        "type": "action",
                        "param": {"model_plan": model_plan},
                    },
                }
            ]
        }
        artifact = {
            "generator": "inlumen-codegen-service",
            "generator_version": "0.1.0",
            "data_contract": {"version": "1"},
        }

        first_hash = inlumen_api._codegen_configuration_hash(
            graph,
            "3",
            artifact,
        )
        graph["nodes"][0]["data"]["param"]["model_plan"][
            "inference_parameters"
        ]["cpu_threads"] = 3
        second_hash = inlumen_api._codegen_configuration_hash(
            graph,
            "3",
            artifact,
        )

        self.assertTrue(first_hash.startswith("sha256:"))
        self.assertTrue(second_hash.startswith("sha256:"))
        self.assertNotEqual(first_hash, second_hash)

    def test_pipeline_codegen_defaults_to_quality_runtime_and_seven_repairs(self):
        payload, metadata = inlumen_api._build_pipeline_codegen_payload(
            {
                "nodes": [
                    {
                        "id": "1",
                        "data": {
                            "label": "Input",
                            "description": "Read input.",
                            "type": "input",
                        },
                    }
                ],
                "edges": [],
            },
            {},
        )

        self.assertEqual(7, payload["options"]["repair_attempts"])
        constraints = payload["context"]["runtime_constraints"]
        self.assertTrue(constraints["allow_unlisted_model_packages"])
        self.assertTrue(constraints["network_allowed"])
        self.assertEqual(900, constraints["max_runtime_seconds"])
        self.assertEqual(payload["options"], metadata["options"])

    def test_codegen_context_fingerprint_detects_background_graph_changes(self):
        context = {
            "graph": {
                "nodes": [{"flow_id": "1", "parameters": {"threshold": 0.5}}],
                "edges": [],
            }
        }

        first = inlumen_api._codegen_context_fingerprint(context)
        context["graph"]["nodes"][0]["parameters"]["threshold"] = 0.8
        second = inlumen_api._codegen_context_fingerprint(context)

        self.assertTrue(first.startswith("sha256:"))
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
