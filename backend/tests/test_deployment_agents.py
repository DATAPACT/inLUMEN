import base64
import io
import sys
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from async_runtime import run_async  # noqa: E402

try:
    from deployment_agents import generate_dockerfiles_with_agent  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional app deps.
    generate_dockerfiles_with_agent = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class DeploymentAgentsTest(unittest.TestCase):
    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_reuses_persisted_codegen_runtime_artifacts_before_llm_fallback(self):
        configuration_hash = "sha256:" + ("a" * 64)
        dockerfile_content = "\n".join(
            [
                "FROM python:3.11-slim",
                "ENV PYTHONUNBUFFERED=1",
                "WORKDIR /app",
                'COPY ["requirements.txt", "/app/requirements.txt"]',
                "RUN pip install --no-cache-dir -r requirements.txt",
                'COPY ["main.py", "/app/main.py"]',
                'COPY ["node-manifest.json", "/app/node-manifest.json"]',
                'CMD ["python", "/app/main.py"]',
                "",
            ]
        )
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 160)
        wav_bytes = wav_buffer.getvalue()
        stored = {
            ("files-step-id-1", "main.py"): b"print('ok')\n",
            ("files-step-id-1", "requirements.txt"): b"pandas\n",
            ("files-step-id-1", "Dockerfile.1"): dockerfile_content.encode("utf-8"),
            (
                "files-step-id-1",
                "node-manifest.json",
            ): (
                b'{"entrypoint":["python","/app/main.py"],'
                b'"data_contract":{"inputs":[{"filename":"input.wav",'
                b'"kind":"binary","format":"wav"}],"outputs":[]}}\n'
            ),
            ("files-step-id-1", "validation-report.json"): b'{"status":"valid"}\n',
            ("files-step-id-1", "input.wav"): wav_bytes,
        }

        async def read_object(bucket, filename):
            return stored[(bucket, filename)]

        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Ingest",
                        "type": "input",
                        "generated_artifact": {
                            "status": "current",
                            "generator": "inlumen-codegen-service",
                            "generator_version": "0.1.0",
                            "configuration_hash": configuration_hash,
                            "entrypoint": ["python", "/app/main.py"],
                            "data_contract": {
                                "inputs": [
                                    {
                                        "filename": "input.wav",
                                        "kind": "binary",
                                        "format": "wav",
                                    }
                                ],
                                "outputs": [],
                            },
                            "validation_report": {"status": "valid"},
                            "files": [
                                {"filename": "main.py", "bucket": "files-step-id-1"},
                                {"filename": "requirements.txt", "bucket": "files-step-id-1"},
                                {"filename": "Dockerfile.1", "bucket": "files-step-id-1"},
                                {"filename": "node-manifest.json", "bucket": "files-step-id-1"},
                                {"filename": "validation-report.json", "bucket": "files-step-id-1"},
                            ],
                        },
                    },
                }
            ],
            "edges": [],
        }

        with patch(
            "deployment_agents.read_minio_object_bytes",
            side_effect=read_object,
        ), patch(
            "deployment_agents.resolve_llm_config",
            side_effect=AssertionError("LLM fallback should not be used"),
        ):
            payload = run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    None,
                    pipeline_graph=graph,
                    file_refs=[
                        {
                            "step_id": "1",
                            "filename": "input.wav",
                            "bucket": "files-step-id-1",
                            "content_type": "audio/wav",
                        }
                    ],
                )
            )

        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        dockerfile = result["dockerfiles"][0]
        self.assertEqual("Dockerfile.1", dockerfile["dockerfile_filename"])
        self.assertEqual(dockerfile_content, dockerfile["content"])
        self.assertEqual(["python", "/app/main.py"], dockerfile["command"])
        self.assertTrue(dockerfile["image"].startswith("ghcr.io/inlumen/codegen-1:"))
        self.assertIn("main.py", result["runtime_artifacts"][0]["files"][0]["filename"])
        self.assertIn(
            "persisted codegen runtime artifacts were reused before Dockerfile fallback",
            result["guardrails"]["checks"],
        )
        self.assertEqual(
            {
                "nodes/1/Dockerfile.1",
                "nodes/1/main.py",
                "nodes/1/node-manifest.json",
                "nodes/1/requirements.txt",
                "nodes/1/validation-report.json",
            },
            {item["path"] for item in result["deployment_files"]},
        )
        audio_file = result["input_files"][0]
        self.assertEqual("1", audio_file["flow_id"])
        self.assertEqual("input.wav", audio_file["filename"])
        self.assertEqual("binary", audio_file["kind"])
        self.assertEqual("wav", audio_file["format"])
        self.assertEqual("base64", audio_file["content_encoding"])
        self.assertEqual(wav_bytes, base64.b64decode(audio_file["content"]))
        self.assertEqual(len(wav_bytes), audio_file["size_bytes"])
        self.assertTrue(audio_file["sha256"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
