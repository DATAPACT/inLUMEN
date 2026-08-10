import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node_definitions.registry import NodeDefinitionRegistry  # noqa: E402
from node_definitions.routes import create_node_definitions_blueprint  # noqa: E402
from node_definitions.schema import NodeDefinitionValidationError  # noqa: E402


class NodeDefinitionRegistryTest(unittest.TestCase):
    def setUp(self):
        self.previous_auth_enabled = os.environ.get("AUTH_ENABLED")
        os.environ["AUTH_ENABLED"] = "false"

    def tearDown(self):
        if self.previous_auth_enabled is None:
            os.environ.pop("AUTH_ENABLED", None)
        else:
            os.environ["AUTH_ENABLED"] = self.previous_auth_enabled

    def test_loads_core_definitions(self):
        registry = NodeDefinitionRegistry()

        definitions = registry.list()
        ids = {definition.id for definition in definitions}

        self.assertEqual(
            {
                "core.source",
                "core.task",
                "core.destination",
                "core.flow",
                "core.subpipeline",
            },
            ids,
        )
        self.assertEqual(
            ["sources", "tasks", "destinations", "flow", "subpipeline"],
            [definition.family for definition in definitions],
        )
        source_node = registry.get("core.source")
        self.assertEqual("source", source_node.base_type)
        self.assertEqual("default", source_node.editor.kind)

    def test_keeps_legacy_definitions_resolvable_but_out_of_the_palette(self):
        registry = NodeDefinitionRegistry()

        legacy = registry.get("core.model-training")

        self.assertIsNotNone(legacy)
        self.assertFalse(legacy.enabled)
        self.assertNotIn(
            "core.model-training",
            {definition.id for definition in registry.list()},
        )
        self.assertIn(
            "core.model-training",
            {definition.id for definition in registry.list(include_disabled=True)},
        )
        self.assertIn(
            "core.sink",
            {definition.id for definition in registry.list(include_disabled=True)},
        )

    def test_rejects_duplicate_definition_ids(self):
        definition = {
            "id": "example.node",
            "version": 1,
            "base_type": "task",
            "family": "example",
            "palette": {
                "label": "Example",
                "description": "",
                "icon": "info",
                "color": "blue",
            },
            "editor": {"kind": "default"},
            "runtime": {"generator": "generic"},
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest_dir = Path(directory)
            (manifest_dir / "one.json").write_text(
                json.dumps({"definitions": [definition]}),
                encoding="utf-8",
            )
            (manifest_dir / "two.json").write_text(
                json.dumps({"definitions": [definition]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                NodeDefinitionValidationError,
                "duplicate node definition id",
            ):
                NodeDefinitionRegistry(manifest_dir)

    def test_discovery_endpoint_returns_versioned_contract(self):
        app = Flask(__name__)
        app.register_blueprint(
            create_node_definitions_blueprint(NodeDefinitionRegistry())
        )
        response = app.test_client().get("/api/node-definitions")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(1, payload["schema_version"])
        self.assertTrue(payload["definitions"])
        self.assertTrue(
            all(definition["version"] >= 1 for definition in payload["definitions"])
        )


if __name__ == "__main__":
    unittest.main()
