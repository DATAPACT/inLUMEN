import ast
import json
import runpy
from pathlib import Path

from fastapi.testclient import TestClient

from app.generator import deterministic_pipeline_payload
from app.main import app

CANONICAL_NODE_TYPES = [
    "source",
    "task",
    "flow",
    "subpipeline",
    "destination",
]


def all_node_types_payload() -> dict:
    subpipeline = {
        "reference": {
            "pipeline_uid": "reusable-normalizer",
            "version_uid": "normalizer-v1",
        },
        "interface": {
            "inputs": [
                {
                    "id": "record",
                    "name": "record",
                    "type": "json",
                    "internal": {"node": "nested-source", "port": "data"},
                }
            ],
            "outputs": [
                {
                    "id": "normalized",
                    "name": "normalized",
                    "type": "json",
                    "internal": {
                        "node": "nested-destination",
                        "port": "data",
                    },
                }
            ],
        },
        "resolved_graph": {
            "nodes": [
                {
                    "id": "nested-source",
                    "data": {
                        "label": "Nested input",
                        "type": "source",
                        "ports": {
                            "inputs": [],
                            "outputs": [{"id": "data", "type": "json"}],
                        },
                    },
                },
                {
                    "id": "nested-task",
                    "data": {
                        "label": "Normalize record",
                        "type": "task",
                        "ports": {
                            "inputs": [{"id": "input", "type": "json"}],
                            "outputs": [{"id": "output", "type": "json"}],
                        },
                    },
                },
                {
                    "id": "nested-destination",
                    "data": {
                        "label": "Nested output",
                        "type": "destination",
                        "ports": {
                            "inputs": [{"id": "data", "type": "json"}],
                            "outputs": [],
                        },
                    },
                },
            ],
            "edges": [
                {
                    "source": "nested-source",
                    "target": "nested-task",
                    "sourceHandle": "data",
                    "targetHandle": "input",
                },
                {
                    "source": "nested-task",
                    "target": "nested-destination",
                    "sourceHandle": "output",
                    "targetHandle": "data",
                },
            ],
        },
    }
    return {
        "context": {
            "pipeline": {"name": "Canonical node type coverage"},
            "graph": {
                "nodes": [
                    {
                        "flow_id": "source",
                        "label": "JSON Source",
                        "type": "source",
                        "ports": {
                            "inputs": [],
                            "outputs": [{"id": "data", "type": "json"}],
                        },
                        "files": [
                            {
                                "filename": "record.json",
                                "kind": "json",
                                "format": "json",
                            }
                        ],
                    },
                    {
                        "flow_id": "task",
                        "label": "Prepare Record",
                        "description": "Prepare the record for routing.",
                        "type": "task",
                        "ports": {
                            "inputs": [{"id": "input", "type": "json"}],
                            "outputs": [{"id": "output", "type": "json"}],
                        },
                    },
                    {
                        "flow_id": "flow",
                        "label": "Approved Route",
                        "description": "Route approved records.",
                        "type": "flow",
                        "template": "Condition",
                        "parameters": {"expression": "value.approved == true"},
                        "ports": {
                            "inputs": [{"id": "value", "type": "json"}],
                            "outputs": [
                                {"id": "when_true", "type": "json"},
                                {"id": "when_false", "type": "json"},
                            ],
                        },
                    },
                    {
                        "flow_id": "subpipeline",
                        "label": "Reusable Normalizer",
                        "description": "Run the pinned normalization pipeline.",
                        "type": "subpipeline",
                        "subpipeline": subpipeline,
                        "ports": {
                            "inputs": [{"id": "record", "type": "json"}],
                            "outputs": [{"id": "normalized", "type": "json"}],
                        },
                    },
                    {
                        "flow_id": "destination",
                        "label": "JSON Destination",
                        "type": "destination",
                        "ports": {
                            "inputs": [{"id": "data", "type": "json"}],
                            "outputs": [],
                        },
                    },
                ],
                "edges": [
                    {
                        "source": "source",
                        "target": "task",
                        "source_port": "data",
                        "target_port": "input",
                    },
                    {
                        "source": "task",
                        "target": "flow",
                        "source_port": "output",
                        "target_port": "value",
                    },
                    {
                        "source": "flow",
                        "target": "subpipeline",
                        "source_port": "when_true",
                        "target_port": "record",
                    },
                    {
                        "source": "subpipeline",
                        "target": "destination",
                        "source_port": "normalized",
                        "target_port": "data",
                    },
                ],
            },
        },
        "options": {
            "allow_deterministic_fallback": False,
            "validation_mode": "static",
        },
        "llm_config": {
            "provider": "test-provider",
            "model": "test-code-model",
            "base_url": "https://model.invalid/v1",
        },
    }


def generated_file(node: dict, filename: str) -> str:
    return next(
        item["content"]
        for item in node["generated_artifact"]["files"]
        if item["filename"] == filename
    )


def test_pipeline_codegen_generates_every_canonical_node_type(
    monkeypatch,
) -> None:
    captured: dict = {}

    async def fake_generate(_config, plan, _user_instruction, _usage_callback=None):
        captured["plan"] = plan
        return deterministic_pipeline_payload(plan)

    monkeypatch.setattr("app.generator.generate_pipeline_payload", fake_generate)

    response = TestClient(app).post(
        "/v1/generate/pipeline-scripts",
        headers={"X-LLM-API-Key": "test-provider-key"},
        json=all_node_types_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["integration_validation"]["status"] == "valid"
    assert body["generation_run"]["status"] == "valid"
    assert [node["flow_id"] for node in body["nodes"]] == CANONICAL_NODE_TYPES

    plan_nodes = captured["plan"]["nodes"]
    assert [node["descriptor"]["type"] for node in plan_nodes] == (
        CANONICAL_NODE_TYPES
    )
    for node in body["nodes"]:
        assert node["generated_artifact"]["validation_report"]["status"] == "valid"
        source = generated_file(node, "main.py")
        ast.parse(source)
        own_function = f"node_{node['flow_id']}"
        assert f"def {own_function}(" in source
        assert all(
            f"def node_{other}(" not in source
            for other in CANONICAL_NODE_TYPES
            if other != node["flow_id"]
        )


def test_flow_and_subpipeline_context_survives_planning_and_compilation(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict = {}

    async def fake_generate(_config, plan, _user_instruction, _usage_callback=None):
        captured["plan"] = plan
        return deterministic_pipeline_payload(plan)

    monkeypatch.setattr("app.generator.generate_pipeline_payload", fake_generate)
    response = TestClient(app).post(
        "/v1/generate/pipeline-scripts",
        headers={"X-LLM-API-Key": "test-provider-key"},
        json=all_node_types_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    plan_by_id = {node["flow_id"]: node for node in captured["plan"]["nodes"]}

    flow = plan_by_id["flow"]
    assert flow["descriptor"]["template"] == "Condition"
    assert flow["descriptor"]["parameters"]["expression"] == (
        "value.approved == true"
    )
    assert [port["id"] for port in flow["descriptor"]["ports"]["outputs"]] == [
        "when_true",
        "when_false",
    ]
    flow_edge = next(
        edge
        for edge in captured["plan"]["edges"]
        if edge["source"] == "flow"
    )
    assert flow_edge["source_port"] == "when_true"
    assert flow_edge["target_port"] == "record"

    subpipeline = plan_by_id["subpipeline"]
    definition = subpipeline["descriptor"]["subpipeline"]
    assert definition["reference"] == {
        "pipeline_uid": "reusable-normalizer",
        "version_uid": "normalizer-v1",
    }
    assert definition["interface"]["inputs"][0]["id"] == "record"
    assert definition["interface"]["outputs"][0]["id"] == "normalized"
    assert [
        node["id"] for node in definition["resolved_graph"]["nodes"]
    ] == ["nested-source", "nested-task", "nested-destination"]
    assert subpipeline["task_profile"]["name"] == "subpipeline"

    generated_by_id = {node["flow_id"]: node for node in body["nodes"]}
    input_path = tmp_path / "record.json"
    input_path.write_text(json.dumps({"approved": True}), encoding="utf-8")
    inputs = [
        {
            "filename": input_path.name,
            "path": str(input_path),
            "kind": "json",
            "format": "json",
        }
    ]
    for flow_id in ("flow", "subpipeline"):
        script_path = tmp_path / f"{flow_id}.py"
        script_path.write_text(
            generated_file(generated_by_id[flow_id], "main.py"),
            encoding="utf-8",
        )
        namespace = runpy.run_path(str(script_path), run_name=f"test_{flow_id}")
        output_dir = tmp_path / f"{flow_id}-outputs"
        outputs = namespace[f"node_{flow_id}"](inputs, output_dir, {})

        assert outputs
        assert all(Path(output["path"]).is_file() for output in outputs)
