import base64
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from async_runtime import run_async  # noqa: E402

try:
    from deployment_agents import (  # noqa: E402
        DeploymentArtifactValidationError,
        _cli_task_contract,
        _cli_task_launcher_source,
        _control_flow_main_source,
        _managed_adapter_main_source,
        _managed_adapter_runtime,
        _task_capability_contract,
        _task_io_contract,
        generate_dockerfiles_with_agent,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional app deps.
    generate_dockerfiles_with_agent = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class DeploymentAgentsTest(unittest.TestCase):
    def test_database_and_object_storage_adapters_have_runtime_contracts(self):
        database_artifact, _ = _managed_adapter_runtime(
            {
                "flow_id": "db",
                "type": "source",
                "template": "Database",
                "param": {
                    "connection_url": "postgresql://user:secret@example/db",
                    "query": "SELECT 1",
                },
                "secret_params": ["connection_url"],
            }
        )
        database_files = {item["filename"]: item for item in database_artifact["files"]}
        self.assertIn("psycopg[binary]", database_files["requirements.txt"]["content"])
        self.assertIn("database_rows.csv", database_artifact["data_contract"]["outputs"][0]["filename"])
        self.assertNotIn("postgresql://user:secret", database_files["main.py"]["content"])
        storage_artifact, _ = _managed_adapter_runtime(
            {
                "flow_id": "minio",
                "type": "destination",
                "template": "Object Storage",
                "param": {"bucket": "inlumen-demo", "prefix": "coverage"},
            }
        )
        storage_files = {item["filename"]: item for item in storage_artifact["files"]}
        self.assertIn("minio", storage_files["requirements.txt"]["content"])
        self.assertEqual(
            [{"name": "input_artifacts", "kind": "artifact"}],
            storage_artifact["data_contract"]["inputs"],
        )

    def test_rest_adapter_uses_optional_api_key_environment(self):
        artifact, _ = _managed_adapter_runtime(
            {
                "flow_id": "weather-api",
                "type": "source",
                "template": "REST API",
                "param": {"url": "https://example.test/weather"},
            }
        )
        files = {item["filename"]: item for item in artifact["files"]}
        source = files["main.py"]["content"]
        manifest = json.loads(files["node-manifest.json"]["content"])
        self.assertIn("_rest_api_source", source)
        self.assertIn('os.getenv("API_KEY", "")', source)
        by_name = {item["name"]: item for item in manifest["runtime_environment"]}
        self.assertFalse(by_name["API_KEY"]["required"])
        self.assertFalse(by_name["API_ENDPOINT"]["required"])

    def test_custom_destination_preserves_filesystem_artifacts(self):
        artifact, _ = _managed_adapter_runtime(
            {
                "flow_id": "custom-output",
                "type": "destination",
                "template": "Custom",
                "param": {},
            }
        )
        main_source = next(
            item["content"] for item in artifact["files"] if item["filename"] == "main.py"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            (input_dir / "data").mkdir(parents=True)
            (input_dir / "data" / "result.json").write_text('{"ok": true}\n', encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "PIPELINE_INPUT_DIR": str(input_dir),
                    "PIPELINE_OUTPUT_DIR": str(output_dir),
                },
                clear=False,
            ):
                namespace = {"__name__": "__main__"}
                exec(compile(main_source, "custom-destination-main.py", "exec"), namespace)

            self.assertEqual(
                '{"ok": true}\n',
                (output_dir / "data" / "result.json").read_text(encoding="utf-8"),
            )
            receipt = json.loads((output_dir / "delivery-receipt.json").read_text())
            self.assertEqual("filesystem", receipt["mode"])

    def test_legacy_json_output_destination_uses_filesystem_sink(self):
        artifact, _ = _managed_adapter_runtime(
            {
                "flow_id": "json-output",
                "type": "destination",
                "template": "JSON Output",
                "param": {},
            }
        )
        main_source = next(
            item["content"] for item in artifact["files"] if item["filename"] == "main.py"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "result.json").write_text('{"risk": "low"}\n', encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "PIPELINE_INPUT_DIR": str(input_dir),
                    "PIPELINE_OUTPUT_DIR": str(output_dir),
                },
                clear=False,
            ):
                namespace = {"__name__": "__main__"}
                exec(compile(main_source, "json-output-main.py", "exec"), namespace)

            self.assertTrue((output_dir / "result.json").is_file())
            self.assertTrue((output_dir / "delivery-receipt.json").is_file())

    def test_legacy_file_output_destination_uses_filesystem_sink(self):
        artifact, _ = _managed_adapter_runtime(
            {
                "flow_id": "file-output",
                "type": "destination",
                "template": "File Output",
                "param": {},
            }
        )
        main_source = next(
            item["content"] for item in artifact["files"] if item["filename"] == "main.py"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "result.txt").write_text("ready\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "PIPELINE_INPUT_DIR": str(input_dir),
                    "PIPELINE_OUTPUT_DIR": str(output_dir),
                },
                clear=False,
            ):
                namespace = {"__name__": "__main__"}
                exec(compile(main_source, "file-output-main.py", "exec"), namespace)

            self.assertEqual("ready\n", (output_dir / "result.txt").read_text(encoding="utf-8"))
            self.assertTrue((output_dir / "delivery-receipt.json").is_file())

    def test_managed_source_adapter_copies_multiple_input_files_as_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "knowledge.pdf"
            pdf_bytes = b"%PDF-1.4\nmanaged adapter fixture\n"
            pdf_path.write_bytes(pdf_bytes)
            questions_path = root / "questions.json"
            questions_path.write_text(
                json.dumps({"questions": ["What is retained?"]}),
                encoding="utf-8",
            )
            output_dir = root / "outputs"
            output_dir.mkdir()
            namespace = {"__name__": "managed_adapter_test"}
            generated_source = _managed_adapter_main_source(
                {"kind": "source", "label": "PDF Knowledge Source"}
            )
            compile(generated_source, "managed-adapter-main.py", "exec")
            exec(generated_source, namespace)

            outputs = namespace["_source_outputs"](
                [
                    {
                        "filename": pdf_path.name,
                        "path": str(pdf_path),
                        "kind": "binary",
                        "format": "pdf",
                    },
                    {
                        "filename": questions_path.name,
                        "path": str(questions_path),
                        "kind": "json",
                        "format": "json",
                    },
                ],
                output_dir,
            )

            self.assertEqual(pdf_bytes, (output_dir / "knowledge.pdf").read_bytes())
            self.assertEqual(
                {"questions": ["What is retained?"]},
                json.loads((output_dir / "questions.json").read_text()),
            )
            self.assertEqual(
                {"knowledge.pdf", "questions.json"},
                {item["filename"] for item in outputs},
            )

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_flow_nodes_get_a_deterministic_runtime_without_main_py(self):
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Input",
                        "type": "source",
                        "template_label": "File",
                    },
                },
                {
                    "id": "5",
                    "data": {
                        "label": "Risk Threshold Check",
                        "type": "flow",
                        "template_label": "Condition",
                        "param": {"expression": "value.risk_score > 0.8"},
                    },
                },
                {
                    "id": "6",
                    "data": {
                        "label": "Alert",
                        "type": "destination",
                        "template_label": "REST API",
                    },
                },
            ],
            "edges": [
                {"source": "1", "target": "5", "targetHandle": "value"},
                {
                    "source": "5",
                    "target": "6",
                    "sourceHandle": "when_true",
                    "targetHandle": "data",
                },
            ],
        }

        payload = run_async(
            generate_dockerfiles_with_agent(
                [],
                [],
                pipeline_graph=graph,
                require_attached_runtime=True,
            )
        )
        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()

        self.assertEqual(["1", "5", "6"], [item["flow_id"] for item in result["dockerfiles"]])
        flow_artifact = next(
            item for item in result["runtime_artifacts"] if item["flow_id"] == "5"
        )
        self.assertEqual("inlumen-control-flow", flow_artifact["generator"])
        self.assertEqual(
            {"main.py", "requirements.txt", "node-manifest.json"},
            {item["filename"] for item in flow_artifact["files"]},
        )
        self.assertIn("value.risk_score > 0.8", flow_artifact["manifest"]["adapter"]["parameters"]["expression"])

    def test_control_flow_runtime_passes_through_filesystem_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "risk.json").write_text('{"risk_score": 0.9}', encoding="utf-8")
            namespace = {"__name__": "control_flow_test"}
            generated_source = _control_flow_main_source(
                {
                    "kind": "flow",
                    "template": "Condition",
                    "parameters": {"expression": "value.risk_score > 0.8"},
                }
            )
            compile(generated_source, "control-flow-main.py", "exec")
            exec(generated_source, namespace)

            with patch.dict(
                os.environ,
                {
                    "PIPELINE_INPUT_DIR": str(input_dir),
                    "PIPELINE_OUTPUT_DIR": str(output_dir),
                },
                clear=False,
            ):
                namespace["main"]()

            self.assertEqual(
                '{"risk_score": 0.9}',
                (output_dir / "risk.json").read_text(encoding="utf-8"),
            )

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_cli_adapter_routes_manifest_inputs_and_discovers_outputs(self):
        user_script = '''
import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--output")
    args = parser.parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "result.json").write_text(
        '{"received": "' + Path(args.input).name + '"}', encoding="utf-8"
    )

if __name__ == "__main__":
    main()
'''
        contract = _cli_task_contract(user_script)
        self.assertEqual("directory", contract["output_kind"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(user_script, encoding="utf-8")
            (root / "cli_launcher.py").write_text(
                _cli_task_launcher_source("cli-task", contract),
                encoding="utf-8",
            )
            audio_path = root / "sample.wav"
            audio_path.write_bytes(b"RIFF")
            input_manifest = root / "input_manifest.json"
            input_manifest.write_text(
                json.dumps({
                    "inputs": [{
                        "filename": "sample.wav",
                        "path": str(audio_path),
                        "format": "wav",
                    }]
                }),
                encoding="utf-8",
            )
            output_dir = root / "outputs"
            output_manifest = output_dir / "output_manifest.json"
            completed = subprocess.run(
                [sys.executable, str(root / "cli_launcher.py")],
                env={
                    **os.environ,
                    "INLUMEN_INPUT_MANIFEST": str(input_manifest),
                    "INLUMEN_OUTPUT_DIR": str(output_dir),
                    "INLUMEN_OUTPUT_MANIFEST": str(output_manifest),
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            manifest = json.loads(output_manifest.read_text(encoding="utf-8"))
            self.assertEqual("result.json", manifest["outputs"][0]["filename"])

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_task_io_contract_is_inferred_or_explicit(self):
        filesystem_contract, filesystem_origin = _task_io_contract(
            "from pathlib import Path\nprint('regular main')\n", []
        )
        self.assertEqual("inferred", filesystem_origin)
        self.assertEqual("filesystem", filesystem_contract["execution"]["adapter"])
        self.assertEqual("directory", filesystem_contract["input"]["delivery"])
        self.assertEqual("scan", filesystem_contract["output"]["discovery"])

        function_contract, function_origin = _task_io_contract(
            "def run(input, params):\n    return input\n", []
        )
        self.assertEqual("inferred", function_origin)
        self.assertEqual("function", function_contract["execution"]["adapter"])
        self.assertEqual("object", function_contract["input"]["delivery"])

        cli_contract, cli_origin = _task_io_contract(
            """
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--input')
parser.add_argument('--output')
""",
            [],
        )
        self.assertEqual("inferred", cli_origin)
        self.assertEqual("cli", cli_contract["execution"]["adapter"])
        self.assertEqual("scan", cli_contract["output"]["discovery"])

        manifest_contract, manifest_origin = _task_io_contract(
            "print('task')\n",
            [{
                "filename": "inlumen.task.json",
                "content": json.dumps({
                    "execution": {"adapter": "manifest"},
                    "input": {"delivery": "manifest"},
                    "output": {"discovery": "manifest", "target": "directory"},
                }),
            }],
        )
        self.assertEqual("declared", manifest_origin)
        self.assertEqual("manifest", manifest_contract["execution"]["adapter"])

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_declared_task_capabilities_are_normalized(self):
        capabilities = _task_capability_contract(
            {
                "dependencies": {"python": ["requests>=2", "requests>=2"]},
                "models": [{
                    "model_id": "owner/reviewed-model",
                    "revision": "0123456789abcdef",
                    "runtime": "local",
                }],
                "resources": {"cpu": 2, "timeout_seconds": 120},
                "secrets": ["API_TOKEN"],
                "side_effects": [{"kind": "api-write", "idempotent": True}],
            },
            {
                "execution": {"adapter": "cli"},
                "input": {"delivery": "file"},
                "output": {"discovery": "scan", "target": "directory"},
            },
            {},
        )
        self.assertEqual("inlumen.task-capability@1", capabilities["schema_version"])
        self.assertEqual(["requests>=2"], capabilities["dependencies"]["python"])
        self.assertEqual("owner/reviewed-model", capabilities["models"][0]["model_id"])

    def test_reviewed_model_system_dependencies_are_allowlisted(self):
        capabilities = _task_capability_contract(
            {},
            {
                "execution": {"adapter": "filesystem"},
                "input": {"delivery": "directory"},
                "output": {"discovery": "scan", "target": "directory"},
            },
            {
                "model_id": "openai/whisper-small",
                "model_revision": "973afd24965f72e36ca33b3055d56a652f456b4d",
                "adapter_id": "huggingface-transformers",
                "required_system_packages": ["ffmpeg"],
            },
        )

        self.assertEqual(["ffmpeg"], capabilities["dependencies"]["system"])

        with self.assertRaises(DeploymentArtifactValidationError):
            _task_capability_contract(
                {"dependencies": {"system": ["curl"]}},
                {
                    "execution": {"adapter": "filesystem"},
                    "input": {"delivery": "directory"},
                    "output": {"discovery": "scan", "target": "directory"},
                },
                {},
            )

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
    def test_invalid_persisted_codegen_artifact_falls_back_to_uploaded_task(self):
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "User task",
                        "type": "task",
                        "generated_artifact": {
                            "status": "current",
                            "generator": "inlumen-codegen-service",
                        },
                        "files": [
                            {"filename": "main.py", "role": "code"},
                            {"filename": "requirements.txt", "role": "code"},
                            {"filename": "node-manifest.json", "role": "code"},
                        ],
                    },
                }
            ],
            "edges": [],
        }
        stale_error = DeploymentArtifactValidationError(
            "Persisted codegen runtime artifact validation failed",
            ["Node 1 has an obsolete validation result."],
        )
        attached_runtime = {
            "flow_id": "1",
            "generator": "inlumen-attached-runtime",
            "files": [],
            "data_contract": {"inputs": [], "outputs": []},
        }
        attached_dockerfile = {
            "flow_id": "1",
            "dockerfile_filename": "Dockerfile.1",
            "content": "\n".join([
                "FROM python:3.11-slim",
                "WORKDIR /app",
                'COPY [\"requirements.txt\", \"/app/requirements.txt\"]',
                "RUN pip install --no-cache-dir -r requirements.txt",
                'COPY [\"main.py\", \"/app/main.py\"]',
                'CMD [\"python\", \"/app/main.py\"]',
            ]),
        }

        with patch(
            "deployment_agents._read_persisted_codegen_artifact",
            side_effect=stale_error,
        ), patch(
            "deployment_agents._read_attached_python_runtime",
            return_value=(attached_runtime, attached_dockerfile),
        ) as read_uploaded:
            payload = run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    pipeline_graph=graph,
                    require_attached_runtime=True,
                )
            )

        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        self.assertEqual(
            "inlumen-attached-runtime",
            result["runtime_artifacts"][0]["generator"],
        )
        self.assertEqual("Dockerfile.1", result["dockerfiles"][0]["dockerfile_filename"])
        read_uploaded.assert_called_once()

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_reuses_persisted_codegen_runtime_and_derives_build_definition(self):
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
        ):
            payload = run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
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
        self.assertIn("uv pip install --system -r requirements.txt", dockerfile["content"])
        self.assertEqual(["python", "/app/main.py"], dockerfile["command"])
        self.assertTrue(dockerfile["image"].startswith("ghcr.io/inlumen/codegen-1:"))
        self.assertIn("main.py", result["runtime_artifacts"][0]["files"][0]["filename"])
        self.assertIn(
            "persisted runtime artifacts were reused before deployment packaging",
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
        ):
            payload = run_async(
                generate_dockerfiles_with_agent(
                    [],
                    [],
                    pipeline_graph=graph,
                )
            )

        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        artifact = result["runtime_artifacts"][0]
        filenames = {item["filename"] for item in artifact["files"]}
        self.assertEqual("inlumen-attached-runtime", artifact["generator"])
        self.assertTrue(
            {"main.py", "requirements.txt", "node-manifest.json"} <= filenames
        )
        self.assertFalse(any(name.startswith("Dockerfile.") for name in filenames))
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
                        "template_label": "File",
                        "param": {"filename": "capture.json"},
                    },
                },
            ],
            "edges": [{"source": "1", "target": "2"}],
        }

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

    @unittest.skipIf(
        generate_dockerfiles_with_agent is None,
        f"deployment agent dependencies are unavailable: {IMPORT_ERROR}",
    )
    def test_user_main_py_takes_precedence_over_a_managed_source_adapter(self):
        stored = {
            ("files-step-id-1", "main.py"): b"print('custom source')\n",
            ("files-step-id-1", "requirements.txt"): b"",
        }

        async def read_object(bucket, filename):
            return stored[(bucket, filename)]

        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Custom upload",
                        "type": "source",
                        "generated_artifact": {
                            "status": "current",
                            "generator": "inlumen-codegen-service",
                            "provenance": {"user_modified": True},
                        },
                        "files": [
                            {"filename": "main.py", "bucket": "files-step-id-1", "role": "code"},
                            {"filename": "requirements.txt", "bucket": "files-step-id-1", "role": "code"},
                        ],
                    },
                },
            ],
            "edges": [],
        }

        with patch(
            "deployment_agents.read_minio_object_bytes",
            side_effect=read_object,
        ):
            payload = run_async(
                generate_dockerfiles_with_agent([], [], pipeline_graph=graph)
            )

        result = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        self.assertEqual(
            "inlumen-attached-runtime",
            result["runtime_artifacts"][0]["generator"],
        )
        main_file = next(
            item
            for item in result["runtime_artifacts"][0]["files"]
            if item["filename"] == "main.py"
        )
        self.assertIn("custom source", main_file["content"])


if __name__ == "__main__":
    unittest.main()
