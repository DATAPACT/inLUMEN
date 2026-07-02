import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
