import asyncio
import json
import unittest
from pathlib import Path
from sys import path
from unittest.mock import AsyncMock, MagicMock, patch


path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics_api import _build_graph_sync_guardrail, _clean_client_graph
from graph_client import run_neo4j_query
from llm_config import LLMConfig
from local_api_client import LocalApiResponse
from pipeline_editor_team import (
    _agent_query_returned_no_rows,
    build_pipeline_editing_team,
    normalize_agent_implementation,
)
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


def flow_node(node_id, template, parameters):
    return {
        "id": str(node_id),
        "type": "custom",
        "position": {"x": 0, "y": 0},
        "data": {
            "type": "flow",
            "label": template,
            "description": template,
            "template_label": template,
            "param": parameters,
        },
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

    def test_condition_flow_uses_template_ports_and_real_branches(self):
        graph = {
            "nodes": [
                node(1, "source", "Input"),
                flow_node(2, "Condition", {"expression": 'value.sentiment == "negative"'}),
                node(3, "task", "Complaint", GENERATED_CODE),
                node(4, "task", "Statistics", GENERATED_CODE),
                node(5, "destination", "Delivery"),
            ],
            "edges": [
                edge(1, 2, "data", "value"),
                edge(2, 3, "when_true", "input"),
                edge(2, 4, "when_false", "input"),
                edge(3, 4, "output", "input"),
                edge(4, 5, "output", "data"),
            ],
        }

        self.assertTrue(validate_pipeline_graph(graph)["valid"])

    def test_parallel_map_uses_items_and_item_ports(self):
        graph = {
            "nodes": [
                node(1, "source", "Image Upload"),
                flow_node(2, "Parallel Map", {
                    "max_concurrency": 4,
                    "failure_policy": "continue",
                }),
                node(3, "task", "Resize Image", GENERATED_CODE),
                node(4, "destination", "Export"),
            ],
            "edges": [
                edge(1, 2, "data", "items"),
                edge(2, 3, "item", "input"),
                edge(3, 4, "output", "data"),
            ],
        }

        self.assertTrue(validate_pipeline_graph(graph)["valid"])

    def test_generic_flow_is_rejected_by_backend_guardrail(self):
        graph = {
            "nodes": [
                node(1, "source", "Input"),
                flow_node(2, "Flow", {}),
                node(3, "destination", "Output"),
            ],
            "edges": [edge(1, 2, "data", "input"), edge(2, 3, "output", "data")],
        }

        report = validate_pipeline_graph(graph)

        self.assertFalse(report["valid"])
        self.assertIn("missing-flow-behavior", {issue["code"] for issue in report["issues"]})


class PipelineAgentGuardrailTest(unittest.TestCase):
    def test_empty_agent_query_result_is_detected_for_live_and_mocked_results(self):
        self.assertTrue(_agent_query_returned_no_rows("[]"))
        self.assertTrue(_agent_query_returned_no_rows([]))
        self.assertFalse(_agent_query_returned_no_rows('[{"connection": {}}]'))

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
        tool_names = [tool.__name__ for tool in assistant_agent.call_args.kwargs["tools"]]
        self.assertIn("connect_steps", tool_names)
        self.assertIn("disconnect_steps", tool_names)
        self.assertIn("configure_flow_step", tool_names)
        system_message = assistant_agent.call_args.kwargs["system_message"]
        self.assertIn("Condition.when_false", system_message)
        self.assertIn('value.sentiment == "negative"', system_message)
        self.assertIn("Complaint has exactly one outgoing edge", system_message)
        self.assertIn("Parallel Map owns iteration", system_message)

    @patch("pipeline_editor_team.RoundRobinGroupChat")
    @patch("pipeline_editor_team.AssistantAgent")
    @patch("pipeline_editor_team.select_model_client")
    @patch("pipeline_editor_team.run_neo4j_query", new_callable=AsyncMock)
    def test_connect_steps_persists_explicit_branch_handles(
        self,
        run_query,
        select_model_client,
        assistant_agent,
        _round_robin,
    ):
        config = LLMConfig(
            provider="openrouter",
            model="test/model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
        select_model_client.return_value = MagicMock()
        run_query.return_value = [{"connection": {"source_port": "when_false"}}]

        build_pipeline_editing_team(config)
        connect_steps = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "connect_steps"
        )
        asyncio.run(connect_steps(json.dumps({
            "source_flow_id": "4",
            "target_flow_id": "6",
            "source_port": "when_false",
            "target_port": "input",
        })))

        query = run_query.await_args.args[0]
        self.assertIn("collect(DISTINCT existingTarget) AS existingTargets", query)
        self.assertIn("source.type = 'flow'", query)
        self.assertIn("OR false", query)
        self.assertIn("MERGE (source)-[flow:FLOWS_TO]->(target)", query)
        self.assertIn("flow.source_port = 'when_false'", query)
        self.assertIn("flow.target_port = 'input'", query)
        self.assertIn("trueTarget.y = coalesce(source.y, 0.0) - 180.0", query)
        self.assertIn("falseTargetIsMerge", query)
        self.assertEqual("connect_steps", run_query.await_args.args[1])

    @patch("pipeline_editor_team.RoundRobinGroupChat")
    @patch("pipeline_editor_team.AssistantAgent")
    @patch("pipeline_editor_team.select_model_client")
    @patch("pipeline_editor_team.run_neo4j_query", new_callable=AsyncMock)
    def test_disconnect_steps_removes_only_the_exact_port_aware_edge(
        self,
        run_query,
        select_model_client,
        assistant_agent,
        _round_robin,
    ):
        config = LLMConfig(
            provider="openrouter",
            model="test/model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
        select_model_client.return_value = MagicMock()
        run_query.return_value = [{"disconnected": {"deleted_connection_count": 1}}]

        build_pipeline_editing_team(config)
        disconnect_steps = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "disconnect_steps"
        )
        asyncio.run(disconnect_steps(json.dumps({
            "source_flow_id": "5",
            "target_flow_id": "7",
            "source_port": "output",
            "target_port": "data",
        })))

        query = run_query.await_args.args[0]
        self.assertIn("MATCH (source)-[flow:FLOWS_TO]->(target)", query)
        self.assertIn("coalesce(flow.source_port, '') = 'output'", query)
        self.assertIn("coalesce(flow.target_port, '') = 'data'", query)
        self.assertIn("DELETE connection", query)
        self.assertEqual("disconnect_steps", run_query.await_args.args[1])

    @patch("pipeline_editor_team.RoundRobinGroupChat")
    @patch("pipeline_editor_team.AssistantAgent")
    @patch("pipeline_editor_team.select_model_client")
    @patch("pipeline_editor_team.run_neo4j_query", new_callable=AsyncMock)
    def test_configure_flow_step_migrates_generic_condition_handles(
        self,
        run_query,
        select_model_client,
        assistant_agent,
        _round_robin,
    ):
        config = LLMConfig(
            provider="openrouter",
            model="test/model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
        select_model_client.return_value = MagicMock()
        run_query.return_value = [{"flow_step": {"behavior": "Condition"}}]

        build_pipeline_editing_team(config)
        configure_flow_step = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "configure_flow_step"
        )
        asyncio.run(configure_flow_step(json.dumps({
            "flow_id": "4",
            "behavior": "Condition",
            "parameters": {"expression": 'value.sentiment == "negative"'},
        })))

        query = run_query.await_args.args[0]
        self.assertIn("flowStep.template_label = 'Condition'", query)
        self.assertIn("connection.target_port = 'value'", query)
        self.assertIn("['when_true', 'when_false']", query)
        self.assertIn("'when_true'", query)
        self.assertEqual("configure_flow_step", run_query.await_args.args[1])

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
