import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analytics_api import _read_run_output_files, _write_validation_bundle
from artifact_content import decode_artifact_content, encode_artifact_bytes


class ArtifactContentTransportTest(unittest.TestCase):
    def test_text_content_remains_plain_utf8(self):
        original = b"name,value\nalpha,1\n"

        encoded = encode_artifact_bytes(
            original,
            filename="sample.csv",
            content_type="text/csv",
        )

        self.assertEqual("utf-8", encoded["content_encoding"])
        self.assertEqual(original.decode("utf-8"), encoded["content"])
        self.assertEqual(original, decode_artifact_content(encoded))

    def test_binary_content_uses_lossless_base64(self):
        original = b"RIFF\xff\x00\x80WAVEfmt \x00data"

        encoded = encode_artifact_bytes(
            original,
            filename="sample.wav",
            content_type="audio/wav",
        )

        self.assertEqual("base64", encoded["content_encoding"])
        self.assertEqual(len(original), encoded["size_bytes"])
        self.assertTrue(encoded["sha256"].startswith("sha256:"))
        self.assertEqual(original, decode_artifact_content(encoded))

    def test_validation_bundle_materialization_preserves_text_and_binary(self):
        csv_bytes = b"name,value\nalpha,1\n"
        wav_bytes = b"RIFF\xff\x00\x80WAVEfmt \x00data"
        files = [
            {
                "path": "inputs/sample.csv",
                "filename": "sample.csv",
                **encode_artifact_bytes(
                    csv_bytes,
                    filename="sample.csv",
                    content_type="text/csv",
                ),
            },
            {
                "path": "inputs/sample.wav",
                "filename": "sample.wav",
                **encode_artifact_bytes(
                    wav_bytes,
                    filename="sample.wav",
                    content_type="audio/wav",
                ),
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "analytics_api.DEPLOYMENT_VALIDATION_WORK_DIR",
            Path(temp_dir),
        ):
            bundle_dir = _write_validation_bundle(files)
            self.assertEqual(
                csv_bytes,
                (bundle_dir / "inputs/sample.csv").read_bytes(),
            )
            self.assertEqual(
                wav_bytes,
                (bundle_dir / "inputs/sample.wav").read_bytes(),
            )

    def test_validation_bundle_rejects_tampered_content(self):
        encoded = encode_artifact_bytes(
            b"original",
            filename="sample.pdf",
            content_type="application/pdf",
        )
        encoded["content"] = encode_artifact_bytes(
            b"tampered",
            filename="sample.pdf",
            content_type="application/pdf",
        )["content"]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "analytics_api.DEPLOYMENT_VALIDATION_WORK_DIR",
            Path(temp_dir),
        ):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                _write_validation_bundle(
                    [
                        {
                            "path": "inputs/sample.pdf",
                            "filename": "sample.pdf",
                            **encoded,
                        }
                    ]
                )

    def test_run_outputs_include_typed_verifiable_descriptors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "outputs" / "node-1"
            output_dir.mkdir(parents=True)
            (output_dir / "report.pdf").write_bytes(b"%PDF-sample")

            [output] = _read_run_output_files(Path(temp_dir))

        self.assertEqual("document", output["kind"])
        self.assertEqual("pdf", output["format"])
        self.assertEqual("application/pdf", output["content_type"])
        self.assertEqual(len(b"%PDF-sample"), output["size_bytes"])
        self.assertRegex(output["sha256"], r"^sha256:[a-f0-9]{64}$")


if __name__ == "__main__":
    unittest.main()
