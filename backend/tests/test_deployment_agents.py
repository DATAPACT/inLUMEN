import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment_agents import (  # noqa: E402
    _dagster_attachment_context,
    _normalize_dagster_packaging_manifest,
)
from deployment_artifacts import DeploymentArtifactValidationError  # noqa: E402


class DagsterPackagingManifestTest(unittest.TestCase):
    def setUp(self):
        self.steps = [{"flow_id": "1"}]
        self.files = [
            {"step_id": "1", "filename": "run.py"},
            {"step_id": "1", "filename": "input.csv"},
        ]

    def test_normalizes_existing_files_and_infers_python(self):
        manifest = _normalize_dagster_packaging_manifest(
            {
                "files": [
                    {
                        "step_id": "1",
                        "source_filename": "run.py",
                        "role": "script",
                        "destination": "scripts/run.py",
                        "args": ["--input", "data/input.csv"],
                    },
                    {
                        "step_id": "1",
                        "source_filename": "input.csv",
                        "role": "input",
                        "destination": "data/input.csv",
                        "args": [],
                    },
                ]
            },
            self.steps,
            self.files,
        )

        self.assertEqual("python", manifest["files"][0]["interpreter"])
        self.assertEqual(
            ["--input", "data/input.csv"],
            manifest["files"][0]["args"],
        )

    def test_rejects_invented_or_missing_files(self):
        with self.assertRaises(DeploymentArtifactValidationError):
            _normalize_dagster_packaging_manifest(
                {
                    "files": [
                        {
                            "step_id": "1",
                            "source_filename": "invented.py",
                            "role": "script",
                            "destination": "scripts/invented.py",
                            "args": [],
                        }
                    ]
                },
                self.steps,
                self.files,
            )

    def test_attachment_context_contains_actual_file_contents(self):
        context = _dagster_attachment_context([
            {
                "step_id": "1",
                "filename": "run.py",
                "content": (
                    b'import argparse\n'
                    b'parser = argparse.ArgumentParser()\n'
                    b'parser.add_argument("--input", default="data/input.csv")\n'
                ),
            },
            {
                "step_id": "1",
                "filename": "input.csv",
                "content": b"patient_id,heart_rate\n1,80\n",
            },
        ])

        self.assertIn('parser.add_argument("--input"', context[0]["content"])
        self.assertIn("patient_id,heart_rate", context[1]["content"])
        self.assertEqual("text", context[0]["content_mode"])
        self.assertEqual(64, len(context[0]["sha256"]))

    def test_attachment_context_rejects_unreadable_attachment(self):
        with self.assertRaises(DeploymentArtifactValidationError):
            _dagster_attachment_context([
                {"step_id": "1", "filename": "run.py"},
            ])


if __name__ == "__main__":
    unittest.main()
