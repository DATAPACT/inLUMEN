import io
import sys
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from async_runtime import run_async  # noqa: E402
from deployment_artifacts import (  # noqa: E402
    DeploymentArtifactValidationError,
    build_deployment_bundle_files,
)

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
    def test_dagster_rejects_input_uploaded_to_wrong_pipeline_node(self):
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(8000)
            wav_file.writeframes(b"\x00\x00" * 80)
        real_wav = wav_buffer.getvalue()
        stored_text = {
            ("files-step-id-1", "main.py"): (
                "from pathlib import Path\n"
                "audio = next(p for p in Path('.').iterdir() if p.suffix == '.wav')\n"
                "Path('transcript.txt').write_text(audio.name)\n"
            ),
            ("files-step-id-2", "main.py"): "print('downstream')\n",
        }
        stored_bytes = {
            **{
                key: value.encode("utf-8")
                for key, value in stored_text.items()
            },
            ("files-step-id-2", "recording.wav"): real_wav,
        }

        async def read_text(bucket, filename):
            return stored_text[(bucket, filename)]

        async def read_bytes(bucket, filename):
            return stored_bytes[(bucket, filename)]

        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Transcribe",
                        "type": "action",
                        "file_buckets": [
                            {"filename": "main.py", "bucket": "files-step-id-1"},
                        ],
                    },
                },
                {
                    "id": "2",
                    "data": {
                        "label": "Analyze",
                        "type": "action",
                        "file_buckets": [
                            {"filename": "main.py", "bucket": "files-step-id-2"},
                            {"filename": "recording.wav", "bucket": "files-step-id-2"},
                        ],
                    },
                },
            ],
            "edges": [{"source": "1", "target": "2"}],
        }

        with patch("deployment_agents.read_minio_object", side_effect=read_text), patch(
            "deployment_agents.read_minio_object_bytes",
            side_effect=read_bytes,
        ), self.assertRaises(DeploymentArtifactValidationError) as raised:
            run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    None,
                    pipeline_graph=graph,
                    file_refs=[],
                    require_attached_runtime=True,
                )
            )

        self.assertIn(
            "recording.wav is attached to node 2 (Analyze), but node 1 "
            "(Transcribe) appears to read it",
            str(raised.exception),
        )

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_dagster_rejects_placeholder_input_before_building_bundle(self):
        fake_wav = (
            b"RIFF0000WAVEfmt 0000FAKEPCM\n"
            b"This is a fake wav file for demos.\n"
        )
        stored_text = {
            ("files-step-id-1", "main.py"): "print('should not run')\n",
            ("files-step-id-1", "input.wav"): fake_wav.decode("utf-8"),
        }
        stored_bytes = {
            ("files-step-id-1", "main.py"): b"print('should not run')\n",
            ("files-step-id-1", "input.wav"): fake_wav,
        }

        async def read_text(bucket, filename):
            return stored_text[(bucket, filename)]

        async def read_bytes(bucket, filename):
            return stored_bytes[(bucket, filename)]

        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Transcribe",
                        "type": "action",
                        "file_buckets": [
                            {"filename": filename, "bucket": bucket}
                            for bucket, filename in stored_text
                        ],
                    },
                }
            ],
            "edges": [],
        }

        with patch("deployment_agents.read_minio_object", side_effect=read_text), patch(
            "deployment_agents.read_minio_object_bytes",
            side_effect=read_bytes,
        ), self.assertRaises(DeploymentArtifactValidationError) as raised:
            run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    None,
                    pipeline_graph=graph,
                    file_refs=[],
                    require_attached_runtime=True,
                )
            )

        self.assertIn("placeholder data", str(raised.exception))
        self.assertIn("not a valid WAV", str(raised.exception))

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_manual_script_requirements_and_input_build_complete_dagster_bundle(self):
        stored_text = {
            ("files-step-id-1", "main.py"): (
                "from pathlib import Path\n"
                "Path('result.txt').write_text(Path('input.txt').read_text().upper())\n"
            ),
            ("files-step-id-1", "requirements.txt"): "requests==2.32.4\n",
            ("files-step-id-1", "input.txt"): "hello\n",
        }

        async def read_text(bucket, filename):
            return stored_text[(bucket, filename)]

        async def read_bytes(bucket, filename):
            return stored_text[(bucket, filename)].encode("utf-8")

        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Plain external script",
                        "type": "action",
                        "file_buckets": [
                            {"filename": filename, "bucket": bucket}
                            for bucket, filename in stored_text
                        ],
                    },
                }
            ],
            "edges": [],
        }

        with patch("deployment_agents.read_minio_object", side_effect=read_text), patch(
            "deployment_agents.read_minio_object_bytes",
            side_effect=read_bytes,
        ), patch(
            "deployment_agents.resolve_llm_config",
            side_effect=AssertionError("Manual attachments should bypass the LLM"),
        ):
            payload = run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    None,
                    pipeline_graph=graph,
                    file_refs=[],
                    require_attached_runtime=True,
                )
            )

        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        dockerfile = result["dockerfiles"][0]
        self.assertEqual("Dockerfile.1", dockerfile["dockerfile_filename"])
        self.assertEqual(["python", "/app/main.py"], dockerfile["command"])
        self.assertIn('COPY ["main.py", "/app/main.py"]', dockerfile["content"])
        by_filename = {
            item["filename"]: item
            for item in result["deployment_files"]
        }
        self.assertEqual("hello\n", by_filename["input.txt"]["content"])
        self.assertEqual(
            "requests==2.32.4\n",
            by_filename["requirements.txt"]["content"],
        )
        self.assertNotIn("node-manifest.json", by_filename)

        bundle = build_deployment_bundle_files(
            graph,
            result,
            targets={"argo": False, "dagster": True},
        )
        by_path = {item["path"]: item["content"] for item in bundle["files"]}
        self.assertEqual(
            stored_text[("files-step-id-1", "main.py")],
            by_path["nodes/node-1-plain-external-script/main.py"],
        )
        self.assertEqual(
            "hello\n",
            by_path["nodes/node-1-plain-external-script/input.txt"],
        )
        self.assertIn(
            '"requests==2.32.4"',
            by_path["dagster/pyproject.toml"],
        )
        self.assertIn("dagster/docker-compose.yml", by_path)

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
        stored = {
            ("files-step-id-1", "main.py"): "print('ok')\n",
            ("files-step-id-1", "requirements.txt"): "pandas\n",
            ("files-step-id-1", "Dockerfile.1"): dockerfile_content,
            (
                "files-step-id-1",
                "node-manifest.json",
            ): '{"entrypoint":["python","/app/main.py"],"data_contract":{"outputs":[]}}\n',
            ("files-step-id-1", "validation-report.json"): '{"status":"valid"}\n',
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
                            "data_contract": {"outputs": []},
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

        with patch("deployment_agents.read_minio_object", side_effect=read_object), patch(
            "deployment_agents.resolve_llm_config",
            side_effect=AssertionError("LLM fallback should not be used"),
        ):
            payload = run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    None,
                    pipeline_graph=graph,
                    file_refs=[],
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

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_dagster_generation_uses_current_node_attachments_and_preserves_binary_inputs(self):
        dockerfile_content = "\n".join(
            [
                "FROM python:3.11-slim",
                "WORKDIR /app",
                'COPY ["requirements.txt", "/app/requirements.txt"]',
                "RUN pip install --no-cache-dir -r requirements.txt",
                'COPY ["ingest_pdf.py", "/app/ingest_pdf.py"]',
                'COPY ["node-manifest.json", "/app/node-manifest.json"]',
                'CMD ["python", "/app/ingest_pdf.py", "--pages", "5"]',
                "",
            ]
        )
        stored_text = {
            ("files-step-id-1", "ingest_pdf.py"): "print('ingest')\n",
            ("files-step-id-1", "requirements.txt"): "pypdf\n",
            ("files-step-id-1", "Dockerfile.1"): dockerfile_content,
            (
                "files-step-id-1",
                "node-manifest.json",
            ): '{"entrypoint":["python","/app/ingest_pdf.py","--pages","5"]}\n',
            ("files-step-id-1", "input.pdf"): "%PDF binary placeholder",
        }
        stored_bytes = {
            key: value.encode("utf-8")
            for key, value in stored_text.items()
        }
        stored_bytes[("files-step-id-1", "input.pdf")] = b"%PDF-\xff\x00binary"

        async def read_text(bucket, filename):
            return stored_text[(bucket, filename)]

        async def read_bytes(bucket, filename):
            return stored_bytes[(bucket, filename)]

        attached_files = [
            {"filename": filename, "bucket": bucket}
            for bucket, filename in stored_text
        ]
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "PDF ingestion",
                        "type": "input",
                        "file_buckets": attached_files,
                    },
                }
            ],
            "edges": [],
        }

        with patch("deployment_agents.read_minio_object", side_effect=read_text), patch(
            "deployment_agents.read_minio_object_bytes",
            side_effect=read_bytes,
        ), patch(
            "deployment_agents.resolve_llm_config",
            side_effect=AssertionError("Attached runtime files should bypass the LLM"),
        ):
            payload = run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    None,
                    pipeline_graph=graph,
                    file_refs=[],
                    require_attached_runtime=True,
                )
            )

        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        by_filename = {
            item["filename"]: item
            for item in result["deployment_files"]
        }
        self.assertEqual(
            ["python", "/app/ingest_pdf.py", "--pages", "5"],
            result["dockerfiles"][0]["command"],
        )
        self.assertEqual("print('ingest')\n", by_filename["ingest_pdf.py"]["content"])
        self.assertEqual("base64", by_filename["input.pdf"]["encoding"])
        self.assertNotIn(
            "input.pdf",
            {
                item["filename"]
                for item in result["runtime_artifacts"][0]["files"]
            },
        )


if __name__ == "__main__":
    unittest.main()
