import asyncio
import json
import unittest
from pathlib import Path
from sys import path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_agent.context import (
    _assistant_message_from_result,
    _clean_client_graph,
    _graph_for_agent_context,
    _looks_like_internal_agent_message,
    _safe_assistant_message,
)
from pipeline_agent.guardrails import _build_graph_sync_guardrail
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


def conversation_subpipeline_definition():
    def nested(node_id, kind, ports, implementation=None):
        data = {
            "type": kind,
            "label": node_id.replace("-", " ").title(),
            "description": node_id,
            "ports": ports,
        }
        if implementation is not None:
            data["implementation"] = implementation
        return {"id": node_id, "type": "custom", "position": {"x": 0, "y": 0}, "data": data}

    graph = {
        "nodes": [
            nested("audio-input", "source", {
                "inputs": [],
                "outputs": [{"id": "audio", "name": "audio", "type": "Audio", "required": True}],
            }),
            nested("transcription", "task", {
                "inputs": [{"id": "audio", "name": "audio", "type": "Audio", "required": True}],
                "outputs": [{"id": "conversation_analysis", "name": "conversation_analysis", "type": "Object", "required": True}],
            }),
            nested("analysis-output", "destination", {
                "inputs": [{"id": "conversation_analysis", "name": "conversation_analysis", "type": "Object", "required": True}],
                "outputs": [],
            }),
        ],
        "edges": [
            edge("audio-input", "transcription", "audio", "audio"),
            edge("transcription", "analysis-output", "conversation_analysis", "conversation_analysis"),
        ],
    }
    return {
        "version": 1,
        "graph": graph,
        "interface": {
            "inputs": [{
                "id": "audio", "name": "audio", "type": "Audio", "required": True,
                "internal": {"node": "audio-input", "port": "audio"},
            }],
            "outputs": [{
                "id": "conversation_analysis", "name": "conversation_analysis", "type": "Object", "required": True,
                "internal": {"node": "analysis-output", "port": "conversation_analysis"},
            }],
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

    def test_unknown_edge_port_reports_the_exact_valid_ids(self):
        graph = {
            "nodes": [
                node(1, "source", "Audio Upload"),
                node(2, "task", "Transcribe Audio", GENERATED_CODE),
            ],
            "edges": [edge(1, 2, "source.data", "task.input")],
        }

        report = validate_pipeline_graph(graph)

        issue = next(
            issue
            for issue in report["issues"]
            if issue["code"] == "unknown-edge-port"
        )
        self.assertIn("source used 'source.data' (valid: data)", issue["message"])
        self.assertIn("target used 'task.input' (valid: input)", issue["message"])
        self.assertIn("do not include component-type prefixes", issue["message"])

    def test_non_python_task_requires_migration(self):
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
        self.assertIn("unsupported-task-implementation", {issue["code"] for issue in report["issues"]})

    def test_uploaded_main_py_satisfies_task_implementation_warning(self):
        task = node(2, "task", "Transform")
        task["data"]["files"] = [{"filename": "main.py", "role": "code"}]
        graph = {
            "nodes": [node(1, "source", "Input"), task, node(3, "destination", "Output")],
            "edges": [edge(1, 2, "data", "input"), edge(2, 3, "output", "data")],
        }

        report = validate_pipeline_graph(graph)

        self.assertNotIn(
            "missing-implementation",
            {issue["code"] for issue in report["issues"]},
        )

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

    def test_subpipeline_validates_pinned_reference_and_public_contract(self):
        definition = conversation_subpipeline_definition()
        definition["version"] = 2
        definition["reference"] = {
            "pipeline_uid": "conversation-pipeline",
            "pipeline_name": "Conversation Understanding",
            "version_uid": "version-1",
            "version_name": "Version 1",
        }
        definition.pop("graph")
        composite = node(2, "subpipeline", "Conversation Understanding")
        composite["data"]["ports"] = {
            "inputs": [{"id": "audio", "name": "audio", "type": "Audio", "required": True}],
            "outputs": [{"id": "conversation_analysis", "name": "conversation_analysis", "type": "Object", "required": True}],
        }
        composite["data"]["subpipeline"] = definition
        graph = {
            "nodes": [node(1, "source", "Audio"), composite, node(3, "destination", "Delivery")],
            "edges": [
                edge(1, 2, "data", "audio"),
                edge(2, 3, "conversation_analysis", "data"),
            ],
        }

        self.assertTrue(validate_pipeline_graph(graph)["valid"])

    def test_subpipeline_requires_reference_and_matching_public_interface(self):
        empty = node(2, "subpipeline", "Empty")
        empty["data"]["subpipeline"] = {}
        empty_report = validate_pipeline_graph({
            "nodes": [node(1, "source", "Input"), empty, node(3, "destination", "Output")],
            "edges": [edge(1, 2, "data", "input"), edge(2, 3, "output", "data")],
        })
        self.assertIn("missing-subpipeline-reference", {issue["code"] for issue in empty_report["issues"]})

        definition = conversation_subpipeline_definition()
        definition["version"] = 2
        definition["reference"] = {
            "pipeline_uid": "conversation-pipeline",
            "pipeline_name": "Conversation Understanding",
            "version_uid": "version-1",
            "version_name": "Version 1",
        }
        definition.pop("graph")
        definition["interface"]["outputs"][0]["id"] = "missing"
        broken = node(2, "subpipeline", "Broken")
        broken["data"]["ports"] = {
            "inputs": [{"id": "audio", "name": "audio", "type": "Audio", "required": True}],
            "outputs": [{"id": "conversation_analysis", "name": "conversation_analysis", "type": "Object", "required": True}],
        }
        broken["data"]["subpipeline"] = definition
        broken_report = validate_pipeline_graph({
            "nodes": [node(1, "source", "Input"), broken, node(3, "destination", "Output")],
            "edges": [edge(1, 2, "data", "audio"), edge(2, 3, "conversation_analysis", "data")],
        })
        self.assertIn("invalid-subpipeline-interface", {issue["code"] for issue in broken_report["issues"]})


class PipelineAgentGuardrailTest(unittest.TestCase):
    def test_only_model_text_messages_can_become_chat_content(self):
        result = SimpleNamespace(messages=[
            SimpleNamespace(
                type="TextMessage",
                source="pipeline_editor",
                content="The selected model's own answer.",
            ),
            SimpleNamespace(
                type="ToolCallSummaryMessage",
                source="pipeline_editor",
                content='{"tool":"overview","params":"{}"}[{"step":{}}]',
            ),
        ])

        self.assertEqual(
            "The selected model's own answer.",
            _assistant_message_from_result(result),
        )

    def test_concatenated_tool_transcript_is_never_returned_as_chat_text(self):
        leaked = (
            '{"tool":"delete_step","params":"{\\"step_uid\\":\\"secret-step\\"}"}'
            '[{"deleted_step":{"pipeline_updated_at":"2026-08-11T13:25:51Z",'
            '"step_uid":"secret-step"}}]'
            '{"tool":"overview","params":"{}"}'
            '[{"step":{"ports_json":"{...}","implementation_json":"{...}"}}]'
            "The pipeline is complete."
        )
        graph = {
            "pipeline": {"label": "Safe Pipeline"},
            "nodes": [node(1, "source", "Input"), node(2, "destination", "Output")],
            "edges": [edge(1, 2, "data", "data")],
        }

        self.assertTrue(_looks_like_internal_agent_message(leaked))
        safe = _safe_assistant_message(leaked, graph)

        self.assertEqual(
            "Current pipeline design: Safe Pipeline contains 2 components and 1 connection.",
            safe,
        )
        self.assertNotIn("delete_step", safe)
        self.assertNotIn("secret-step", safe)
        self.assertNotIn("ports_json", safe)

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

    def test_agent_context_exposes_only_high_level_design_fields(self):
        graph = {
            "nodes": [{
                **node(1, "source", "Audio Upload"),
                "data": {
                    **node(1, "source", "Audio Upload")["data"],
                    "template_label": "REST API",
                    "configuration_status": "unconfigured",
                    "endpoint": "https://stale.example.test",
                    "param": {"language": "en", "api_key": "secret"},
                    "secret_params": ["api_key"],
                },
            }],
            "edges": [],
        }

        summary = _graph_for_agent_context(graph)["nodes"][0]

        self.assertEqual("REST API", summary["template"])
        self.assertNotIn("endpoint", summary)
        self.assertNotIn("parameters", summary)
        self.assertNotIn("implementation", summary)
        self.assertEqual("unconfigured", summary["configuration_status"])

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
        self.assertTrue(any(
            "managed Python package" in message
            for message in sync["validation_errors"]
        ))

    def test_guardrail_accepts_a_valid_model_chosen_graph_change(self):
        before = {
            "nodes": [node(1, "source", "Input")],
            "edges": [],
        }
        after = {
            "nodes": [
                node(1, "source", "Input"),
                node(2, "destination", "Unexpected Output"),
            ],
            "edges": [edge(1, 2, "data", "data")],
        }

        sync = _build_graph_sync_guardrail(
            before,
            after,
            "Is this a good design? What would you improve?",
        )

        self.assertEqual("synced", sync["status"])
        self.assertTrue(sync["guardrail_passed"])
        self.assertTrue(sync["graph_safe_to_apply"])

    def test_guardrail_rejects_missing_explicit_outlier_branch(self):
        before = {"nodes": [], "edges": []}
        after = {
            "nodes": [
                node(1, "source", "Input"),
                node(2, "task", "Validate Rows", GENERATED_CODE),
                flow_node(3, "Condition", {}),
                node(4, "task", "Relative Features", GENERATED_CODE),
                node(5, "destination", "Clean CSV"),
            ],
            "edges": [
                edge(1, 2, "data", "input"),
                edge(2, 3, "output", "value"),
                edge(3, 4, "when_true", "input"),
                edge(4, 5, "output", "data"),
            ],
        }

        sync = _build_graph_sync_guardrail(
            before,
            after,
            "Split into two branches: a clean-data branch saved to CSV and an "
            "outlier branch saved to a separate CSV.",
        )

        self.assertFalse(sync["guardrail_passed"])
        self.assertTrue(any(
            "when_true and when_false" in message
            for message in sync["validation_errors"]
        ))

    def test_guardrail_rejects_unchanged_missing_explicit_outlier_branch(self):
        graph = {
            "nodes": [
                node(1, "source", "Input"),
                flow_node(2, "Condition", {"expression": "value.is_valid == true"}),
                node(3, "destination", "Clean CSV"),
            ],
            "edges": [
                edge(1, 2, "data", "value"),
                edge(2, 3, "when_true", "data"),
            ],
        }

        sync = _build_graph_sync_guardrail(
            graph,
            graph,
            "Split into two branches: a clean-data branch saved to CSV and an "
            "outlier branch saved to a separate CSV.",
        )

        self.assertEqual("invalid", sync["status"])
        self.assertFalse(sync["graph_changed"])
        self.assertFalse(sync["guardrail_passed"])

    def test_guardrail_accepts_generic_parallel_two_branch_destinations(self):
        before = {"nodes": [], "edges": []}
        after = {
            "nodes": [
                node(1, "source", "Telemetry"),
                node(2, "task", "Route Telemetry", GENERATED_CODE),
                node(3, "destination", "Storage"),
                node(4, "destination", "Monitoring"),
            ],
            "edges": [
                edge(1, 2, "data", "input"),
                edge(2, 3, "output", "data"),
                edge(2, 4, "output", "data"),
            ],
        }

        sync = _build_graph_sync_guardrail(
            before,
            after,
            "Fan out telemetry into two branches and save each to a separate output.",
        )

        self.assertEqual("synced", sync["status"])
        self.assertTrue(sync["guardrail_passed"])

    def test_guardrail_does_not_treat_branch_advice_as_a_mutation_contract(self):
        graph = {
            "nodes": [
                node(1, "source", "Telemetry"),
                node(2, "destination", "Storage"),
            ],
            "edges": [edge(1, 2, "data", "data")],
        }

        sync = _build_graph_sync_guardrail(
            graph,
            graph,
            "Do I need two branches here, or is the current single output better?",
        )

        self.assertEqual("unchanged", sync["status"])
        self.assertTrue(sync["guardrail_passed"])

    def test_guardrail_accepts_two_distinct_requested_destinations(self):
        before = {"nodes": [], "edges": []}
        after = {
            "nodes": [
                node(1, "source", "Input"),
                node(2, "task", "Validate Rows", GENERATED_CODE),
                flow_node(3, "Condition", {}),
                node(4, "task", "Relative Features", GENERATED_CODE),
                node(5, "destination", "Clean CSV"),
                node(6, "destination", "Outlier CSV"),
            ],
            "edges": [
                edge(1, 2, "data", "input"),
                edge(2, 3, "output", "value"),
                edge(3, 4, "when_true", "input"),
                edge(4, 5, "output", "data"),
                edge(3, 6, "when_false", "data"),
            ],
        }

        sync = _build_graph_sync_guardrail(
            before,
            after,
            "Split into two branches: a clean-data branch saved to CSV and an "
            "outlier branch saved to a separate CSV.",
        )

        self.assertTrue(sync["guardrail_passed"])

    def test_guardrail_rejects_upstream_bypass_into_branch_target(self):
        before = {"nodes": [], "edges": []}
        after = {
            "nodes": [
                node(1, "source", "Input"),
                node(2, "task", "Validate Rows", GENERATED_CODE),
                flow_node(3, "Condition", {}),
                node(4, "task", "Relative Features", GENERATED_CODE),
                node(5, "destination", "Clean CSV"),
                node(6, "destination", "Outlier CSV"),
            ],
            "edges": [
                edge(1, 2, "data", "input"),
                edge(2, 3, "output", "value"),
                edge(2, 4, "output", "input"),
                edge(3, 4, "when_true", "input"),
                edge(4, 5, "output", "data"),
                edge(3, 6, "when_false", "data"),
            ],
        }

        sync = _build_graph_sync_guardrail(
            before,
            after,
            "Split into two branches: a clean-data branch saved to CSV and an "
            "outlier branch saved to a separate CSV.",
        )

        self.assertFalse(sync["guardrail_passed"])
        self.assertTrue(any(
            "upstream bypass" in message
            for message in sync["validation_errors"]
        ))

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
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
        self.assertIn("list_pipeline_components", tool_names)
        self.assertIn("connect_steps", tool_names)
        self.assertIn("disconnect_steps", tool_names)
        self.assertIn("configure_flow_step", tool_names)
        self.assertIn("configure_subpipeline_step", tool_names)
        self.assertIn("list_reusable_pipelines", tool_names)
        self.assertIn("create_reusable_pipeline", tool_names)
        system_message = assistant_agent.call_args.kwargs["system_message"]
        self.assertIn("never `source.data`", system_message)
        self.assertIn("source_port `when_false`", system_message)
        self.assertIn("configure its executable expression", system_message)
        self.assertIn("value.is_valid == true", system_message)
        self.assertIn("Complaint has exactly one outgoing edge", system_message)
        self.assertIn("owns iteration", system_message)
        self.assertIn("another distinct saved PIPELINE", system_message)
        self.assertIn("Conversation Understanding", system_message)
        self.assertIn("create_step pins the reference", system_message)
        self.assertIn("authoritative five structural boxes", system_message)
        list_components = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "list_pipeline_components"
        )
        component_catalog = json.loads(asyncio.run(list_components("{}")))
        self.assertEqual(
            {"source", "task", "destination", "flow", "subpipeline"},
            {component["type"] for component in component_catalog["components"]},
        )
        self.assertTrue(all(
            "default_implementation" not in component
            for component in component_catalog["components"]
        ))

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_overview_exposes_design_fields_without_runtime_configuration(
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
        run_query.return_value = json.dumps([])

        build_pipeline_editing_team(config)
        overview = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "overview"
        )
        asyncio.run(overview())

        query = run_query.await_args.args[0]
        self.assertIn(".ports_json", query)
        self.assertNotIn("s AS step", query)
        self.assertNotIn("HAS_FILE", query)
        self.assertNotIn("implementation", query)
        self.assertNotIn("param_json", query)

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_assistant_boundaries_always_use_zero_configuration_default(
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
        run_query.return_value = json.dumps([{"step": {"flow_id": "1"}}])

        build_pipeline_editing_team(config)
        create_step = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "create_step"
        )
        asyncio.run(create_step(json.dumps({
            "type": "source",
            "label": "City Data CSV",
            "description": "Receives city records.",
            "template": "File",
        })))

        query = run_query.await_args.args[0]
        self.assertIn("template_label:'Custom'", query)
        self.assertIn("param_json: '{}'", query)
        self.assertNotIn("template_label:'File'", query)

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_create_subpipeline_step_atomically_pins_saved_version_and_public_ports(
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
        run_query.side_effect = [
            json.dumps([{"reusable_pipeline": {
                "pipeline_uid": "reusable-1",
                "pipeline_name": "Conversation Understanding",
                "version_uid": "version-2",
                "version_name": "Version 2",
                "interface_json": json.dumps({
                    "inputs": [{"id": "audio", "type": "Audio", "required": True}],
                    "outputs": [{"id": "conversation_analysis", "type": "Object", "required": True}],
                }),
                "public_ports_json": json.dumps({
                    "inputs": [{"id": "audio", "name": "audio", "type": "Audio", "required": True}],
                    "outputs": [{"id": "conversation_analysis", "name": "conversation_analysis", "type": "Object", "required": True}],
                }),
            }}]),
            json.dumps([{"step": {"flow_id": "2", "referenced_version_uid": "version-2"}}]),
        ]

        build_pipeline_editing_team(config)
        create_step = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "create_step"
        )
        result = asyncio.run(create_step(json.dumps({
            "type": "subpipeline",
            "label": "Conversation Understanding",
            "description": "Reuse saved conversation analysis.",
            "reusable_pipeline_uid": "reusable-1",
            "reusable_version_uid": "version-2",
        })))

        lookup_query = run_query.await_args_list[0].args[0]
        create_query = run_query.await_args_list[1].args[0]
        self.assertIn("rp.uid = 'reusable-1'", lookup_query)
        self.assertIn("rv.uid = 'version-2'", lookup_query)
        self.assertEqual(
            "resolve_reusable_pipeline_for_creation",
            run_query.await_args_list[0].args[1],
        )
        self.assertIn("subpipeline_json:", create_query)
        self.assertIn("primary_input_port: 'audio'", create_query)
        self.assertIn("primary_output_port: 'conversation_analysis'", create_query)
        self.assertIn("flow.target_port = 'audio'", create_query)
        self.assertIn(
            "WHEN prev.type = 'subpipeline' THEN coalesce(prev.primary_output_port, 'output')",
            create_query,
        )
        self.assertIn("referenced_version_uid: 'version-2'", create_query)
        self.assertIn("version-2", result)

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_create_step_infers_parallel_map_from_an_unambiguous_label(
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
        run_query.return_value = json.dumps([{"step": {"flow_id": "2", "type": "flow"}}])

        build_pipeline_editing_team(config)
        create_step = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "create_step"
        )
        asyncio.run(create_step(json.dumps({
            "type": "flow",
            "label": "Process Invoices Parallel",
            "description": "Process each invoice in the batch independently.",
        })))

        query = run_query.await_args.args[0]
        self.assertIn("template_label:'Parallel Map'", query)
        self.assertIn("definition_id:'core.flow'", query)
        self.assertIn("definition_version:1", query)
        self.assertIn("OPTIONAL MATCH (p)-[:HAS_STEP]->(candidateTail:STEP)", query)
        self.assertIn("WHERE NOT (candidate)-[:FLOWS_TO]->()", query)
        self.assertIn("CASE WHEN size(tails) = 1 THEN head(tails)", query)
        self.assertIn("configuration_status:'unconfigured'", query)
        self.assertIn("param_json: '{}'", query)
        self.assertNotIn("max_concurrency", query)
        self.assertNotIn("failure_policy", query)
        self.assertIn("flow.target_port = 'items'", query)

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_create_step_can_atomically_create_false_branch_destination(
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
        run_query.side_effect = [
            [{
                "predecessor": {
                    "pipeline_uid": "active-design-pipeline",
                    "flow_id": "3",
                    "type": "flow",
                    "template_label": "Condition",
                    "ports_json": json.dumps({
                        "inputs": [{"id": "value"}],
                        "outputs": [
                            {"id": "when_true"},
                            {"id": "when_false"},
                        ],
                    }),
                    "primary_output_port": "when_true",
                },
            }],
            [{"step": {"flow_id": "7", "label": "Save Outlier CSV"}}],
        ]

        build_pipeline_editing_team(config)
        create_step = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "create_step"
        )
        asyncio.run(create_step(json.dumps({
            "type": "destination",
            "label": "Save Outlier CSV",
            "description": "Write flagged rows and rejection reasons.",
            "after_flow_id": "3",
            "source_port": "when_false",
        })))

        self.assertEqual(
            "resolve_step_predecessor",
            run_query.await_args_list[0].args[1],
        )
        predecessor_query = run_query.await_args_list[0].args[0]
        self.assertIn("WITH collect(candidate)[0] AS p", predecessor_query)
        self.assertIn("pipeline_uid: p.uid", predecessor_query)
        query = run_query.await_args_list[1].args[0]
        self.assertIn(
            "MATCH (p:PIPELINE {uid:'active-design-pipeline', status:'design'})",
            query,
        )
        self.assertIn("prev.type = 'flow'", query)
        self.assertIn("flow.source_port = 'when_false'", query)
        self.assertIn("flow.target_port = 'data'", query)
        self.assertIn("THEN prevY + 180.0", query)

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_insert_step_preserves_typed_connection_handles(
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
        run_query.return_value = [{"step": {"flow_id": "3"}}]

        build_pipeline_editing_team(config)
        insert_step = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "insert_step"
        )
        asyncio.run(insert_step(json.dumps({
            "type": "task",
            "label": "Validate Records",
            "description": "Validate each incoming record.",
            "after_flow_id": "1",
            "before_flow_id": "2",
        })))

        query = run_query.await_args.args[0]
        self.assertIn("MATCH (p:PIPELINE {status:'design'})", query)
        self.assertIn("oldFlow.source_port AS oldSourcePort", query)
        self.assertIn("oldFlow.target_port AS oldTargetPort", query)
        self.assertIn("MERGE (after)-[incomingFlow:FLOWS_TO]->(s)", query)
        self.assertIn("incomingFlow.target_port = 'input'", query)
        self.assertIn("MERGE (s)-[outgoingFlow:FLOWS_TO]->(before)", query)
        self.assertIn("outgoingFlow.source_port = 'output'", query)
        self.assertNotIn("implementation_json", query)
        self.assertIn("param_json: '{}'", query)
        self.assertEqual("insert_between_steps", run_query.await_args.args[1])

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_delete_step_only_bridges_a_simple_chain_and_preserves_ports(
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
        run_query.return_value = [{"deleted_step": {"step_uid": "step-2"}}]

        build_pipeline_editing_team(config)
        delete_step = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "delete_step"
        )
        asyncio.run(delete_step(json.dumps({"step_uid": "step-2"})))

        query = run_query.await_args.args[0]
        self.assertIn("MATCH (p:PIPELINE {status:'design'})", query)
        self.assertIn("WHEN size(incoming) = 1 AND size(outgoing) = 1", query)
        self.assertIn("incoming[0].source_port", query)
        self.assertIn("outgoing[0].target_port", query)
        self.assertNotIn("FOREACH (p IN prevs", query)
        self.assertEqual("delete_step", run_query.await_args.args[1])

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
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
        run_query.side_effect = [
            [{
                "connection_context": {
                    "source": {
                        "flow_id": "4",
                        "type": "flow",
                        "template_label": "Condition",
                        "ports_json": json.dumps({
                            "inputs": [{"id": "value"}],
                            "outputs": [{"id": "when_true"}, {"id": "when_false"}],
                        }),
                    },
                    "target": {
                        "flow_id": "6",
                        "type": "task",
                        "ports_json": json.dumps({
                            "inputs": [{"id": "input"}],
                            "outputs": [{"id": "output"}],
                        }),
                    },
                },
            }],
            [{"connection": {"source_port": "when_false"}}],
        ]

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
        self.assertEqual("resolve_connection_ports", run_query.await_args_list[0].args[1])

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_connect_steps_normalizes_gpt_oss_type_prefixed_ports(
        self,
        run_query,
        select_model_client,
        assistant_agent,
        _round_robin,
    ):
        config = LLMConfig(
            provider="openrouter",
            model="openai/gpt-oss-120b",
            base_url="https://example.test/v1",
            api_key="secret",
        )
        select_model_client.return_value = MagicMock()
        run_query.side_effect = [
            [{
                "connection_context": {
                    "source": {
                        "flow_id": "1",
                        "type": "source",
                        "ports_json": json.dumps({
                            "inputs": [],
                            "outputs": [{"id": "data"}],
                        }),
                    },
                    "target": {
                        "flow_id": "2",
                        "type": "task",
                        "ports_json": json.dumps({
                            "inputs": [{"id": "input"}],
                            "outputs": [{"id": "output"}],
                        }),
                    },
                },
            }],
            [{"connection": {"source_port": "data", "target_port": "input"}}],
        ]

        build_pipeline_editing_team(config)
        connect_steps = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "connect_steps"
        )
        asyncio.run(connect_steps(json.dumps({
            "source_flow_id": "1",
            "target_flow_id": "2",
            "source_port": "source.data",
            "target_port": "task.input",
        })))

        mutation_query = run_query.await_args_list[1].args[0]
        self.assertIn("flow.source_port = 'data'", mutation_query)
        self.assertIn("flow.target_port = 'input'", mutation_query)
        self.assertNotIn("source.data", mutation_query)
        self.assertNotIn("task.input", mutation_query)

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_connect_steps_rejects_unknown_port_before_mutating(
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
        run_query.return_value = [{
            "connection_context": {
                "source": {
                    "flow_id": "2",
                    "type": "task",
                    "ports_json": json.dumps({
                        "inputs": [{"id": "input"}],
                        "outputs": [{"id": "output"}],
                    }),
                },
                "target": {
                    "flow_id": "3",
                    "type": "task",
                    "ports_json": json.dumps({
                        "inputs": [{"id": "input"}],
                        "outputs": [{"id": "output"}],
                    }),
                },
            },
        }]

        build_pipeline_editing_team(config)
        connect_steps = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "connect_steps"
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Valid output port ids: output",
        ):
            asyncio.run(connect_steps(json.dumps({
                "source_flow_id": "2",
                "target_flow_id": "3",
                "source_port": "task.result",
                "target_port": "task.input",
            })))

        self.assertEqual(1, run_query.await_count)
        self.assertEqual("resolve_connection_ports", run_query.await_args.args[1])

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_connect_steps_infers_single_ports_when_omitted(
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
        run_query.side_effect = [
            [{
                "connection_context": {
                    "source": {
                        "flow_id": "3",
                        "type": "task",
                        "ports_json": json.dumps({
                            "inputs": [{"id": "input"}],
                            "outputs": [{"id": "output"}],
                        }),
                    },
                    "target": {
                        "flow_id": "4",
                        "type": "destination",
                        "ports_json": json.dumps({
                            "inputs": [{"id": "data"}],
                            "outputs": [],
                        }),
                    },
                },
            }],
            [{"connection": {"source_port": "output", "target_port": "data"}}],
        ]

        build_pipeline_editing_team(config)
        connect_steps = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "connect_steps"
        )
        asyncio.run(connect_steps(json.dumps({
            "source_flow_id": "3",
            "target_flow_id": "4",
        })))

        mutation_query = run_query.await_args_list[1].args[0]
        self.assertIn("flow.source_port = 'output'", mutation_query)
        self.assertIn("flow.target_port = 'data'", mutation_query)

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
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

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
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
            "expression": "value.is_valid == true",
        })))

        query = run_query.await_args.args[0]
        self.assertIn("flowStep.template_label = 'Condition'", query)
        self.assertIn("connection.target_port = 'value'", query)
        self.assertIn("['when_true', 'when_false']", query)
        self.assertIn("'when_true'", query)
        self.assertIn(
            'flowStep.param_json = \'{"expression": "value.is_valid == true"}\'',
            query,
        )
        self.assertIn("THEN 'configured'", query)
        self.assertEqual("configure_flow_step", run_query.await_args.args[1])

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_configure_subpipeline_step_pins_saved_version_and_migrates_handles(
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
        run_query.side_effect = [
            json.dumps([{"reusable_pipeline": {
                "pipeline_uid": "reusable-1",
                "pipeline_name": "Conversation Understanding",
                "version_uid": "version-1",
                "version_name": "Version 1",
                "interface_json": json.dumps({
                    "inputs": [{"id": "audio", "name": "audio", "type": "Audio", "required": True, "internal": {"node": "audio-input", "port": "audio"}}],
                    "outputs": [{"id": "conversation_analysis", "name": "conversation_analysis", "type": "Object", "required": True, "internal": {"node": "analysis-output", "port": "conversation_analysis"}}],
                }),
                "public_ports_json": json.dumps({
                    "inputs": [{"id": "audio", "name": "audio", "type": "Audio", "required": True}],
                    "outputs": [{"id": "conversation_analysis", "name": "conversation_analysis", "type": "Object", "required": True}],
                }),
            }}]),
            json.dumps([{"subpipeline_context": {
                "current_ports_json": json.dumps({
                    "inputs": [{"id": "input", "name": "input", "type": "any", "required": True}],
                    "outputs": [{"id": "output", "name": "output", "type": "any", "required": True}],
                }),
                "connected_inputs": ["input"],
                "connected_outputs": ["output"],
            }}]),
            json.dumps([{"subpipeline_step": {"referenced_version_uid": "version-1"}}]),
        ]

        build_pipeline_editing_team(config)
        configure_subpipeline_step = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "configure_subpipeline_step"
        )
        asyncio.run(configure_subpipeline_step(json.dumps({
            "flow_id": "2",
            "pipeline_uid": "reusable-1",
            "version_uid": "version-1",
        })))

        query = run_query.await_args.args[0]
        self.assertIn("subpipelineStep.subpipeline_json", query)
        self.assertIn("subpipelineStep.ports_json", query)
        self.assertIn("subpipelineStep.primary_input_port = 'audio'", query)
        self.assertIn(
            "subpipelineStep.primary_output_port = 'conversation_analysis'",
            query,
        )
        self.assertIn("WHEN connection.target_port = 'input' THEN 'audio'", query)
        self.assertIn("WHEN connection.source_port = 'output' THEN 'conversation_analysis'", query)
        self.assertIn("input_port_mapping:{\"input\": \"audio\"}", query)
        self.assertIn("output_port_mapping:{\"output\": \"conversation_analysis\"}", query)
        self.assertIn("referenced_version_uid:'version-1'", query)
        self.assertEqual("resolve_reusable_pipeline", run_query.await_args_list[0].args[1])
        self.assertEqual("inspect_subpipeline_contract", run_query.await_args_list[1].args[1])
        self.assertEqual("configure_subpipeline_step", run_query.await_args.args[1])

    @patch("pipeline_agent.team.RoundRobinGroupChat")
    @patch("pipeline_agent.team.AssistantAgent")
    @patch("pipeline_agent.team.select_model_client")
    @patch("pipeline_agent.tools.run_neo4j_query", new_callable=AsyncMock)
    def test_create_reusable_pipeline_creates_separate_versioned_pipeline(
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
        run_query.side_effect = [
            json.dumps([]),
            json.dumps([{"reusable_pipeline": {
                "pipeline_uid": "reusable-1",
                "version_uid": "version-1",
            }}]),
        ]

        build_pipeline_editing_team(config)
        create_reusable_pipeline = next(
            tool
            for tool in assistant_agent.call_args.kwargs["tools"]
            if tool.__name__ == "create_reusable_pipeline"
        )
        definition = conversation_subpipeline_definition()
        asyncio.run(create_reusable_pipeline(json.dumps({
            "name": "Conversation Understanding",
            "description": "Reusable conversation analysis.",
            "version_name": "Version 1",
            "graph": definition["graph"],
        })))

        query = run_query.await_args.args[0]
        self.assertIn("status:'reusable'", query)
        self.assertIn("CREATE (v:PIPELINE_VERSION", query)
        self.assertIn("interface_json", query)
        self.assertIn("public_ports_json", query)
        self.assertIn("HAS_VERSION", query)
        self.assertEqual("find_reusable_pipeline_by_name", run_query.await_args_list[0].args[1])
        self.assertEqual("create_reusable_pipeline", run_query.await_args.args[1])

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
