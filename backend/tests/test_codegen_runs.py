import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegen_runs import CodegenRunStore
import inlumen_api


class CodegenRunStoreTests(unittest.TestCase):
    def test_run_metadata_survives_a_new_store_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            first = CodegenRunStore(str(path))
            first.put(
                {
                    "run_id": "run-1",
                    "status": "running",
                    "graph": {"nodes": [{"id": "1"}], "edges": []},
                    "metadata": {"generation_mode": "full"},
                    "created_at": "2026-08-10T10:00:00Z",
                    "updated_at": "2026-08-10T10:00:01Z",
                }
            )

            second = CodegenRunStore(str(path))
            restored = second.get("run-1")

            self.assertIsNotNone(restored)
            self.assertEqual("running", restored["status"])
            self.assertEqual("1", restored["graph"]["nodes"][0]["id"])

    def test_recent_runs_are_returned_newest_first(self):
        store = CodegenRunStore(":memory:")
        store.put(
            {
                "run_id": "older",
                "status": "valid",
                "created_at": "2026-08-10T10:00:00Z",
                "updated_at": "2026-08-10T10:00:01Z",
            }
        )
        store.put(
            {
                "run_id": "newer",
                "status": "running",
                "created_at": "2026-08-10T10:01:00Z",
                "updated_at": "2026-08-10T10:01:01Z",
            }
        )

        self.assertEqual(
            ["newer", "older"],
            [record["run_id"] for record in store.list(limit=10)],
        )

    def test_workspace_cleanup_clears_gateway_and_private_service_history(self):
        store = CodegenRunStore(":memory:")
        store.put({"run_id": "old-run", "status": "valid"})

        with (
            patch.object(inlumen_api, "CODEGEN_RUN_STORE", store),
            patch.object(
                inlumen_api,
                "_clear_codegen_pipeline_runs_request",
                return_value={"status": "cleared", "deleted_count": 1},
            ) as remote_clear,
        ):
            result = inlumen_api._clear_codegen_run_history()

        self.assertEqual([], store.list(limit=10))
        self.assertEqual("cleared", result["status"])
        self.assertEqual(1, result["deleted_count"])
        remote_clear.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
