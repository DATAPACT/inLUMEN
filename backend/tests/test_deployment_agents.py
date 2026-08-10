import base64
import inspect
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
    def test_attached_runtime_option_is_part_of_the_public_signature(self):
        self.assertIn(
            "require_attached_runtime",
            inspect.signature(generate_dockerfiles_with_agent).parameters,
        )
        parameter = inspect.signature(
            generate_dockerfiles_with_agent
        ).parameters["require_attached_runtime"]
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
        self.assertFalse(parameter.default)

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
                        "type": "task",
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

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_enriches_attached_main_py_without_llm_for_any_export_target(self):
        stored = {
            ("files-step-id-1", "main.py"): b"print('ok')\n",
            ("files-step-id-1", "patients.csv"): b"patient_id\n1\n",
        }

        async def read_object(bucket, filename):
            return stored[(bucket, filename)]

        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Load patients",
                        "type": "task",
                        "file_buckets": [
                            {
                                "filename": "main.py",
                                "bucket": "files-step-id-1",
                                "role": "code",
                            },
                            {
                                "filename": "patients.csv",
                                "bucket": "files-step-id-1",
                                "role": "data",
                            },
                        ],
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
                )
            )

        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        artifact = result["runtime_artifacts"][0]
        filenames = {item["filename"] for item in artifact["files"]}
        self.assertEqual("inlumen-attached-runtime", artifact["generator"])
        self.assertTrue(
            {"main.py", "requirements.txt", "node-manifest.json", "Dockerfile.1"}
            <= filenames
        )
        self.assertIn("uv pip install --system", result["dockerfiles"][0]["content"])
        self.assertEqual("patients.csv", result["input_files"][0]["filename"])
        self.assertEqual("table", result["input_files"][0]["kind"])
        self.assertEqual(["1"], result["root_flow_ids"])

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_source_and_destination_use_managed_adapters_without_llm_or_user_code(self):
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Upload",
                        "type": "source",
                        "template_label": "File",
                    },
                },
                {
                    "id": "2",
                    "data": {
                        "label": "Capture",
                        "type": "destination",
                        "template_label": "Notification",
                    },
                },
            ],
            "edges": [{"source": "1", "target": "2"}],
        }

        with patch(
            "deployment_agents.resolve_llm_config",
            side_effect=AssertionError("managed adapters must not use the LLM"),
        ):
            payload = run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    pipeline_graph=graph,
                    require_attached_runtime=True,
                )
            )

        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        self.assertEqual(["1", "2"], [item["flow_id"] for item in result["dockerfiles"]])
        self.assertEqual(
            {"inlumen-managed-adapter"},
            {item["generator"] for item in result["runtime_artifacts"]},
        )
        self.assertTrue(
            all(
                "main.py" in {file_item["filename"] for file_item in artifact["files"]}
                for artifact in result["runtime_artifacts"]
            )
        )


if __name__ == "__main__":
    unittest.main()
