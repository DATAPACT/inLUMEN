import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node_secrets import (  # noqa: E402
    clear_node_secrets,
    configured_node_secrets,
    delete_node_secret,
    runtime_secret_environment,
    runtime_secret_name,
    set_node_secret,
)


class NodeSecretsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = os.environ.get("INLUMEN_SECRET_DB_PATH")
        self.previous_key_path = os.environ.get("INLUMEN_SECRET_KEY_PATH")
        os.environ["INLUMEN_SECRET_DB_PATH"] = str(Path(self.temp_dir.name) / "secrets.sqlite3")
        os.environ["INLUMEN_SECRET_KEY_PATH"] = str(Path(self.temp_dir.name) / "secrets.key")

    def tearDown(self):
        clear_node_secrets()
        if self.previous_db_path is None:
            os.environ.pop("INLUMEN_SECRET_DB_PATH", None)
        else:
            os.environ["INLUMEN_SECRET_DB_PATH"] = self.previous_db_path
        if self.previous_key_path is None:
            os.environ.pop("INLUMEN_SECRET_KEY_PATH", None)
        else:
            os.environ["INLUMEN_SECRET_KEY_PATH"] = self.previous_key_path
        self.temp_dir.cleanup()

    def test_stores_values_outside_the_graph_and_returns_only_status(self):
        set_node_secret("source-1", "api_key", "very-secret")

        self.assertEqual(["api_key"], configured_node_secrets("source-1"))
        self.assertEqual(
            {"INLUMEN_SECRET_SOURCE_1_API_KEY": "very-secret"},
            runtime_secret_environment({
                "nodes": [{
                    "id": "source-1",
                    "data": {
                        "param": {"api_key": ""},
                        "secret_params": ["api_key"],
                    },
                }],
            }),
        )
        self.assertTrue(delete_node_secret("source-1", "api_key"))
        self.assertEqual([], configured_node_secrets("source-1"))
        self.assertNotIn("very-secret", (Path(self.temp_dir.name) / "secrets.sqlite3").read_bytes().decode("latin-1"))

    def test_rejects_invalid_parameter_names(self):
        with self.assertRaises(ValueError):
            set_node_secret("source-1", "../token", "secret")
        self.assertEqual(
            "INLUMEN_SECRET_SOURCE_1_ACCESS_TOKEN",
            runtime_secret_name("source-1", "access-token"),
        )


if __name__ == "__main__":
    unittest.main()
