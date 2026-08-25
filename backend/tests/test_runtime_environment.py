import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_environment import discover_runtime_environment, merge_runtime_environment


class RuntimeEnvironmentTest(unittest.TestCase):
    def test_required_and_optional_environment_are_distinguished(self):
        values = discover_runtime_environment(
            '''import os
endpoint = os.environ["API_ENDPOINT"]
key = os.getenv("API_KEY")
region = os.environ.get("REGION", "eu-west-1")
input_dir = os.environ["PIPELINE_INPUT_DIR"]
'''
        )
        self.assertEqual(
            [
                {"name": "API_ENDPOINT", "required": True, "secret": False, "source": "static-python-analysis"},
                {"name": "API_KEY", "required": False, "secret": True, "source": "static-python-analysis"},
                {"name": "REGION", "required": False, "secret": False, "source": "static-python-analysis"},
            ],
            values,
        )

    def test_manifest_declaration_overrides_static_advice(self):
        merged = merge_runtime_environment(
            discover_runtime_environment('import os\nos.getenv("API_KEY")\n'),
            [{"name": "API_KEY", "required": True, "secret": True}],
        )
        self.assertTrue(merged[0]["required"])
        self.assertEqual("node-manifest", merged[0]["source"])

    def test_dynamic_and_invalid_python_are_ignored_safely(self):
        self.assertEqual([], discover_runtime_environment('os.getenv(name)'))
        self.assertEqual([], discover_runtime_environment('def broken('))


if __name__ == "__main__":
    unittest.main()
