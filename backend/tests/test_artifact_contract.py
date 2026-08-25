import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artifact_contract import artifact_bindings, classify_artifact, normalize_port_id


class ArtifactContractTest(unittest.TestCase):
    def test_normalizes_port_aware_bindings_deterministically(self):
        bindings = artifact_bindings(
            [
                {
                    "source": "source-b",
                    "source_port": "rows out",
                    "target": "merge",
                    "target_port": "right",
                },
                {
                    "source": "source-a",
                    "source_port": "data",
                    "target": "merge",
                    "target_port": "left",
                },
            ]
        )
        self.assertEqual(
            [
                {
                    "source_node": "source-a",
                    "source_port": "data",
                    "target_node": "merge",
                    "target_port": "left",
                },
                {
                    "source_node": "source-b",
                    "source_port": "rows-out",
                    "target_node": "merge",
                    "target_port": "right",
                },
            ],
            [binding.as_dict() for binding in bindings],
        )

    def test_port_ids_are_safe_for_workspace_paths(self):
        self.assertEqual("weather-data", normalize_port_id(" weather data "))
        self.assertEqual("input", normalize_port_id("", "input"))

    def test_supported_format_matrix(self):
        expected = {
            "audio.wav": ("audio", "wav"),
            "document.pdf": ("document", "pdf"),
            "image.png": ("image", "png"),
            "notes.txt": ("text", "txt"),
            "records.json": ("json", "json"),
            "records.jsonl": ("json", "jsonl"),
            "rows.csv": ("table", "csv"),
            "rows.parquet": ("table", "parquet"),
            "workbook.xlsx": ("table", "xlsx"),
            "video.mp4": ("video", "mp4"),
            "archive.zip": ("binary", "zip"),
        }
        for filename, (kind, file_format) in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(
                    {"kind": kind, "format": file_format},
                    classify_artifact(filename),
                )

    def test_declared_canonical_kind_is_preserved(self):
        self.assertEqual(
            {"kind": "model", "format": "pkl"},
            classify_artifact("classifier.pkl", kind="model"),
        )

    def test_legacy_file_kind_is_reclassified(self):
        self.assertEqual(
            {"kind": "audio", "format": "wav"},
            classify_artifact("recording.wav", kind="file", file_format="wav"),
        )


if __name__ == "__main__":
    unittest.main()
