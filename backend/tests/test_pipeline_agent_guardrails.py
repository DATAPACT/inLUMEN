import asyncio
import unittest
from pathlib import Path
from sys import path
from unittest.mock import MagicMock, patch


path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics_api import _build_graph_sync_guardrail, _clean_client_graph
from graph_client import run_neo4j_query
from llm_config import LLMConfig
from local_api_client import LocalApiResponse
from pipeline_editor_team import build_pipeline_editing_team, normalize_agent_implementation
from pipeline_graph_validation import validate_pipeline_graph


def node(node_id, kind, label, implementation=None):
    data = {"type": kind, "label": label, "description": label}
    if implementation is not None:
        data["implementation"] = implementation
    return {"id": str(node_id), "type": "custom", "position": {"x": 0, "y": 0}, "data": data}


def edge(source, target, source_port, target_port):
    return {
        "id": f"e-{source}-{target}",
        "source": str(source),
        "target": str(target),
        "sourceHandle": source_port,
        "targetHandle": target_port,
    }


GENERATED_CODE = {
    "kind": "generated-code",
    "task": "deterministic pipeline task",
    "execution_profile": "deterministic",
}


class PipelineGraphValidationTest(unittest.TestCase):
    def test_patient_risk_example_is_valid(self):
        graph = {
            "nodes": [
                node(1, "source", "Device Ingestion"),
                node(2, "task", "Preprocessing", GENERATED_CODE),
                node(3, "task", "Model Training", {"kind": "python", "task": "model training"}),
                node(4, "destination", "Alerting"),
            ],
            "edges": [
                edge(1, 2, "data", "input"),
                edge(2, 3, "output", "input"),
                edge(3, 4, "output", "data"),
            ],
        }

        self.assertTrue(validate_pipeline_graph(graph)["valid"])

    def test_document_retrieval_example_is_valid_with_intermediate_vector_task(self):
        graph = {
            "nodes": [
                node(1, "source", "PDF Ingestion"),
                node(2, "task", "Content Chunking", GENERATED_CODE),
                node(3, "task", "Embedding Generation", {"kind": "python", "task": "embeddings"}),
                node(4, "task", "Vector Index", GENERATED_CODE),
                node(5, "task", "Question Answering", {"kind": "python", "task": "RAG question answering"}),
                node(6, "destination", "Answer Delivery"),
            ],
            "edges": [
                edge(1, 2, "data", "input"),
                edge(2, 3, "output", "input"),
                edge(3, 4, "output", "input"),
                edge(4, 5, "output", "input"),
                edge(5, 6, "output", "data"),
            ],
        }

        self.assertTrue(validate_pipeline_graph(graph)["valid"])

    def test_destination_cannot_feed_question_answering(self):
        graph = {
            "nodes": [
                node(1, "source", "PDF Ingestion"),
                node(2, "destination", "Vector Store"),
                node(3, "task", "Question Answering", GENERATED_CODE),
                node(4, "destination", "Answer Delivery"),
            ],
            "edges": [
                edge(1, 2, "data", "data"),
                edge(2, 3, "", "input"),
                edge(3, 4, "output", "data"),
            ],
        }

        report = validate_pipeline_graph(graph)

        self.assertFalse(report["valid"])
        self.assertIn("missing-edge-port", {issue["code"] for issue in report["issues"]})

    def test_rest_api_task_requires_a_real_endpoint(self):
        graph = {
            "nodes": [
                node(1, "source", "Input"),
                node(2, "task", "Embedding API", {"kind": "rest-api", "task": "embeddings"}),
                node(3, "destination", "Output"),
            ],
            "edges": [edge(1, 2, "data", "input"), edge(2, 3, "output", "data")],
        }

        report = validate_pipeline_graph(graph)

        self.assertFalse(report["valid"])
        self.assertIn("missing-endpoint", {issue["code"] for issue in report["issues"]})


class PipelineAgentGuardrailTest(unittest.TestCase):
    def test_semantic_implementation_class_is_not_persisted_as_runtime_kind(self):
        normalized = normalize_agent_implementation({
            "kind": "trusted-pretrained-inference",
            "task": "embeddings",
        })

        self.assertEqual("generated-code", normalized["kind"])
        self.assertEqual("trusted_heavy_model", normalized["execution_profile"])

    def test_canvas_cleaning_preserves_execution_metadata_and_port_handles(self):
        graph = {
            "updated_at": "2026-08-10T12:00:00Z",
            "nodes": [
                node(1, "source", "Input"),
                {
                    **node(2, "task", "Transform", GENERATED_CODE),
                    "data": {
                        **node(2, "task", "Transform", GENERATED_CODE)["data"],
                        "ports": {
                            "inputs": [{"id": "records", "name": "records", "type": "Dataset", "required": True}],
                            "outputs": [{"id": "clean", "name": "clean", "type": "Dataset", "required": True}],
                        },
                        "param": {"threshold": 0.7},
                        "secret_params": [],
                        "template_label": "Data Cleaning",
                    },
                },
            ],
            "edges": [edge(1, 2, "data", "records")],
        }

        cleaned = _clean_client_graph(graph)

        self.assertEqual(GENERATED_CODE, cleaned["nodes"][1]["implementation"])
        self.assertEqual(0.7, cleaned["nodes"][1]["param"]["threshold"])
        self.assertEqual("Data Cleaning", cleaned["nodes"][1]["template_label"])
        self.assertEqual("data", cleaned["edges"][0]["source_port"])
        self.assertEqual("records", cleaned["edges"][0]["target_port"])

    def test_guardrail_rejects_a_changed_but_invalid_graph(self):
        before = {"nodes": [], "edges": []}
        after = {
            "nodes": [
                node(1, "source", "Input"),
                node(2, "task", "Broken REST task", {"kind": "rest-api"}),
                node(3, "destination", "Output"),
            ],
            "edges": [edge(1, 2, "data", "input"), edge(2, 3, "output", "data")],
        }

        sync = _build_graph_sync_guardrail(before, after, "Create a pipeline")

        self.assertFalse(sync["guardrail_passed"])
        self.assertFalse(sync["graph_safe_to_apply"])
        self.assertEqual("invalid", sync["status"])
        self.assertTrue(any("endpoint" in message for message in sync["validation_errors"]))

    @patch("pipeline_editor_team.RoundRobinGroupChat")
    @patch("pipeline_editor_team.AssistantAgent")
    @patch("pipeline_editor_team.select_model_client")
    def test_pipeline_tools_are_configured_for_serial_calls(
        self,
        select_model_client,
        assistant_agent,
        round_robin,
    ):
        config = LLMConfig(
            provider="openrouter",
            model="test/model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
        select_model_client.return_value = MagicMock()

        build_pipeline_editing_team(config)

        select_model_client.assert_called_once_with(config, parallel_tool_calls=False)
        assistant_agent.assert_called_once()
        round_robin.assert_called_once()

    @patch("graph_client.dispatch_graph_request")
    def test_graph_client_raises_http_failures_as_tool_errors(self, dispatch_graph_request):
        dispatch_graph_request.return_value = LocalApiResponse(
            content=b'{"error":"deadlock"}',
            status_code=500,
            headers={},
        )

        with self.assertRaisesRegex(RuntimeError, "create_step.*500.*deadlock"):
            asyncio.run(run_neo4j_query("RETURN 1", "create_step"))


if __name__ == "__main__":
    unittest.main()
