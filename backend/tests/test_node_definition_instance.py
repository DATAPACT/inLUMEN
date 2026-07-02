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

    def test_legacy_node_has_no_definition_properties(self):
        properties = {"label": "Legacy", "implementation": {"ignored": True}}

        normalize_definition_properties(properties)

        self.assertEqual({"label": "Legacy"}, properties)
        self.assertEqual({}, definition_data_from_properties(properties))


if __name__ == "__main__":
    unittest.main()
