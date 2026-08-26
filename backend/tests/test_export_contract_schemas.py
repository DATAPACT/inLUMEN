import json
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
CONTRACT_V2_ROOT = REPOSITORY_ROOT / "contracts" / "v2"
CONTRACT_V3_ROOT = REPOSITORY_ROOT / "contracts" / "v3"
sys.path.insert(0, str(BACKEND_ROOT))

from artifact_contract import CANONICAL_ARTIFACT_KINDS  # noqa: E402


class ExportContractSchemaTest(unittest.TestCase):
    def load(self, filename: str, *, version: int = 2):
        root = CONTRACT_V3_ROOT if version == 3 else CONTRACT_V2_ROOT
        with (root / filename).open(encoding="utf-8") as handle:
            return json.load(handle)

    def test_published_contracts_use_json_schema_2020_12_and_unique_ids(self):
        schemas = [
            self.load("project.schema.json"),
            self.load("deployment-bundle.schema.json"),
            self.load("artifact-contract.schema.json"),
            self.load("run-spec.schema.json"),
            self.load("run-result.schema.json"),
            self.load("node-output-manifest.schema.json"),
            self.load("deployment-bundle.schema.json", version=3),
            self.load("artifact-contract.schema.json", version=3),
            self.load("run-spec.schema.json", version=3),
        ]

        self.assertTrue(
            all(
                schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
                for schema in schemas
            )
        )
        self.assertEqual(len(schemas), len({schema["$id"] for schema in schemas}))
        self.assertEqual(
            {
                "inlumen.project@2",
                "inlumen.deployment-bundle@1",
                "inlumen.artifact-contract@2",
                "inlumen.run-spec@2",
                "inlumen.run-result@1",
                "inlumen.output-manifest@1",
                "inlumen.deployment-bundle@2",
                "inlumen.artifact-contract@3",
                "inlumen.run-spec@3",
            },
            {
                schema["properties"]["schema_version"]["const"]
                for schema in schemas
            },
        )

    def test_artifact_vocabulary_is_shared_with_runtime_classification(self):
        schema = self.load("node-output-manifest.schema.json")
        schema_kinds = set(
            schema["$defs"]["artifact"]["properties"]["kind"]["enum"]
        )
        self.assertEqual(set(CANONICAL_ARTIFACT_KINDS), schema_kinds)

        example = self.load("examples/node-output-manifest.json")
        self.assertEqual(
            schema_kinds,
            {entry["kind"] for entry in example["outputs"]},
        )

    def test_task_workspace_contract_is_flat_and_not_port_namespaced(self):
        schema = self.load("artifact-contract.schema.json", version=3)
        properties = schema["properties"]
        self.assertEqual(
            "<artifact-relative-path>", properties["input_layout"]["const"]
        )
        self.assertEqual(
            "<artifact-relative-path>", properties["output_layout"]["const"]
        )
        self.assertFalse(properties["port_namespaced"]["const"])

    def test_legacy_v2_artifact_contract_remains_published_unchanged(self):
        properties = self.load("artifact-contract.schema.json")["properties"]
        self.assertEqual("<input_port>/...", properties["input_layout"]["const"])
        self.assertEqual("<output_port>/...", properties["output_layout"]["const"])
        self.assertNotIn("port_namespaced", properties)


if __name__ == "__main__":
    unittest.main()
