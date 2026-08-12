import json
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "v2"
sys.path.insert(0, str(BACKEND_ROOT))

from artifact_contract import CANONICAL_ARTIFACT_KINDS  # noqa: E402


class ExportContractSchemaTest(unittest.TestCase):
    def load(self, filename: str):
        with (CONTRACT_ROOT / filename).open(encoding="utf-8") as handle:
            return json.load(handle)

    def test_published_contracts_use_json_schema_2020_12_and_unique_ids(self):
        schemas = [
            self.load("project.schema.json"),
            self.load("deployment-bundle.schema.json"),
            self.load("run-result.schema.json"),
            self.load("node-output-manifest.schema.json"),
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
                "inlumen.run-result@1",
                "inlumen.output-manifest@1",
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


if __name__ == "__main__":
    unittest.main()
