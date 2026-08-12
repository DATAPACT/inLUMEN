import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artifact_contract import classify_artifact


class ArtifactContractTest(unittest.TestCase):
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
