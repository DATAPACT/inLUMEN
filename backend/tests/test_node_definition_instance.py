import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node_definitions.instance import (  # noqa: E402
    definition_data_from_properties,
    definition_properties_from_data,
    normalize_definition_properties,
)
from node_definitions.artifacts import configuration_hash  # noqa: E402


class NodeDefinitionInstanceTest(unittest.TestCase):
    def test_round_trips_definition_data_through_neo4j_properties(self):
        data = {
            "definition_id": "core.data-preprocessing",
            "definition_version": 1,
            "configuration_status": "unconfigured",
            "implementation": {
                "kind": "codegen",
                "operation": "preprocess",
                "service_id": "",
                "parameters": {"normalize": True},
            },
        }

        properties = definition_properties_from_data(data)
        restored = definition_data_from_properties(properties)

        self.assertEqual(data, restored)
        self.assertIsInstance(properties["implementation_json"], str)
        self.assertEqual(
            data["implementation"],
            json.loads(properties["implementation_json"]),
        )

    def test_normalizer_removes_nested_map_before_neo4j_write(self):
        properties = {
            "definition_id": "core.model-training",
            "definition_version": "2",
            "configuration_status": "not-a-status",
            "implementation": {"kind": "codegen"},
        }

        normalize_definition_properties(properties)

        self.assertNotIn("implementation", properties)
        self.assertNotIn("configuration_status", properties)
        self.assertEqual(2, properties["definition_version"])
        self.assertEqual({"kind": "codegen"}, json.loads(properties["implementation_json"]))

    def test_round_trips_codegen_configuration(self):
        data = {
            "definition_id": "core.output",
            "definition_version": 1,
            "configuration_status": "valid",
            "implementation": {
                "kind": "codegen",
                "operation": "alert",
                "parameters": {"threshold": 0.75},
            },
        }

        restored = definition_data_from_properties(
            definition_properties_from_data(data)
        )

        self.assertEqual(data, restored)

    def test_implementation_is_independent_of_a_registered_template(self):
        properties = {"label": "Generic Task", "implementation": {"kind": "shell"}}

        normalize_definition_properties(properties)

        self.assertEqual("Generic Task", properties["label"])
        self.assertNotIn("implementation", properties)
        self.assertEqual({"kind": "shell"}, json.loads(properties["implementation_json"]))
        self.assertEqual(
            {"implementation": {"kind": "shell"}},
            definition_data_from_properties(properties),
        )

    def test_round_trips_template_source_and_reusable_pipeline_reference(self):
        data = {
            "template": {"id": "source.rest-api", "name": "REST API"},
            "source_config": {"base_url": "https://example.test"},
            "subpipeline": {
                "version": 2,
                "reference": {
                    "pipeline_uid": "reusable-1",
                    "pipeline_name": "Conversation Understanding",
                    "version_uid": "version-1",
                    "version_name": "Version 1",
                },
                "interface": {"inputs": [], "outputs": []},
                "resolved_graph": {"nodes": [{"id": "transient"}], "edges": []},
            },
        }

        properties = definition_properties_from_data(data)
        restored = definition_data_from_properties(properties)

        self.assertEqual(data["template"], restored["template"])
        self.assertEqual(data["source_config"], restored["source_config"])
        self.assertEqual(data["subpipeline"]["reference"], restored["subpipeline"]["reference"])
        self.assertNotIn("resolved_graph", restored["subpipeline"])
        self.assertTrue(all(key.endswith("_json") for key in properties))

    def test_preserves_legacy_embedded_graph_until_it_is_converted(self):
        legacy_graph = {"nodes": [{"id": "legacy-step"}], "edges": []}

        properties = definition_properties_from_data({
            "subpipeline": {"version": 1, "graph": legacy_graph, "expanded": False},
        })
        restored = definition_data_from_properties(properties)

        self.assertEqual(1, restored["subpipeline"]["version"])
        self.assertEqual(legacy_graph, restored["subpipeline"]["graph"])

    def test_dynamic_model_plan_marks_artifact_stale_when_revision_changes(self):
        model_plan = {
            "framework": "transformers",
            "model_id": "example/custom-model",
            "model_revision": "revision-1",
        }
        artifact_hash = configuration_hash(
            definition_id="inlumen.dynamic-step.3",
            definition_version=1,
            implementation=model_plan,
            generator="inlumen-codegen-service",
            generator_version="0.1.0",
            contract_version="1",
        )
        properties = {
            "flow_id": "3",
            "param_json": json.dumps({"model_plan": model_plan}),
            "generated_artifact_json": json.dumps(
                {
                    "configuration_hash": artifact_hash,
                    "generator": "inlumen-codegen-service",
                    "generator_version": "0.1.0",
                    "data_contract": {"version": "1"},
                }
            ),
        }

        current = definition_data_from_properties(properties)
        self.assertEqual(model_plan, current["implementation"])
        self.assertEqual("current", current["generated_artifact"]["status"])

        properties["param_json"] = json.dumps(
            {
                "model_plan": {
                    **model_plan,
                    "model_revision": "revision-2",
                }
            }
        )
        stale = definition_data_from_properties(properties)
        self.assertEqual("stale", stale["generated_artifact"]["status"])


if __name__ == "__main__":
    unittest.main()
