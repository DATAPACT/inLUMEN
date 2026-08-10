import tempfile
import unittest
from pathlib import Path

from codegen_runs import CodegenRunStore


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


if __name__ == "__main__":
    unittest.main()
