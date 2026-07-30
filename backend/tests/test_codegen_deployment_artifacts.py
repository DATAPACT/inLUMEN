import base64
import hashlib
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment_artifacts import (
    DeploymentArtifactValidationError,
    build_argo_workflow_object,
    build_argo_workflow_yaml,
    build_dagster_project_files,
    build_deployment_bundle_files,
)


def dockerfile_content():
    return "\n".join([
        "FROM python:3.11-slim",
        "WORKDIR /app",
        'COPY ["requirements.txt", "/app/requirements.txt"]',
        "RUN pip install --no-cache-dir -r requirements.txt",
        'COPY ["main.py", "/app/main.py"]',
        'COPY ["node-manifest.json", "/app/node-manifest.json"]',
        'CMD ["python", "/app/main.py"]',
        "",
    ])


def node_manifest_content():
    return json.dumps(
        {
            "data_contract": {
                "inputs": [
                    {
                        "name": "vital_signs_short.csv",
                        "filename": "vital_signs_short.csv",
                        "kind": "table",
                        "format": "csv",
                    },
                    {
                        "name": "sample.wav",
                        "filename": "sample.wav",
                        "kind": "binary",
                        "format": "wav",
                    },
                ]
            }
        },
        indent=2,
    ) + "\n"


def codegen_payload():
    content = dockerfile_content()
    audio_bytes = b"RIFF\xff\x00\x80WAVEfmt \x00data"
    return {
        "dockerfiles": [
            {
                "dockerfile_filename": "Dockerfile.1",
                "content": content,
                "flow_id": "1",
                "image": "ghcr.io/inlumen/codegen-1:bbbbbbbbbbbb",
                "command": ["python", "/app/main.py"],
                "files": ["requirements.txt", "main.py", "node-manifest.json"],
                "generator": "inlumen-codegen-service",
            },
            {
                "dockerfile_filename": "Dockerfile.2",
                "content": content,
                "flow_id": "2",
                "image": "ghcr.io/inlumen/codegen-2:bbbbbbbbbbbb",
                "command": ["python", "/app/main.py"],
                "files": ["requirements.txt", "main.py", "node-manifest.json"],
                "generator": "inlumen-codegen-service",
            },
        ],
        "deployment_files": [
            {"path": "nodes/1/main.py", "filename": "main.py", "flow_id": "1", "content": "print('ingest')\n"},
            {"path": "nodes/1/requirements.txt", "filename": "requirements.txt", "flow_id": "1", "content": "pandas\n# comment\n-r extra.txt\n"},
            {"path": "nodes/1/node-manifest.json", "filename": "node-manifest.json", "flow_id": "1", "content": node_manifest_content()},
            {"path": "nodes/1/vital_signs_short.csv", "filename": "vital_signs_short.csv", "flow_id": "1", "content": "heart_rate\n72\n"},
            {
                "path": "nodes/1/sample.wav",
                "filename": "sample.wav",
                "flow_id": "1",
                "content": base64.b64encode(audio_bytes).decode("ascii"),
                "content_encoding": "base64",
                "content_type": "audio/wav",
                "size_bytes": len(audio_bytes),
                "sha256": f"sha256:{hashlib.sha256(audio_bytes).hexdigest()}",
            },
            {"path": "nodes/2/main.py", "filename": "main.py", "flow_id": "2", "content": "print('preprocess')\n"},
            {"path": "nodes/2/requirements.txt", "filename": "requirements.txt", "flow_id": "2", "content": "pandas\nnumpy\n"},
            {"path": "nodes/2/node-manifest.json", "filename": "node-manifest.json", "flow_id": "2", "content": "{}\n"},
        ],
    }


class CodegenDeploymentArtifactsTest(unittest.TestCase):
    def graph(self):
        return {
            "nodes": [
                {"id": "1", "data": {"label": "Ingestion", "type": "input"}},
                {"id": "2", "data": {"label": "Preprocessing", "type": "action"}},
            ],
            "edges": [{"source": "1", "target": "2"}],
        }

    def test_codegen_dockerfile_payload_selects_manifest_handoff_without_node_metadata(self):
        workflow = build_argo_workflow_object(self.graph(), codegen_payload())
        self.assertEqual("inlumen-codegen-", workflow["metadata"]["generateName"])
        self.assertEqual(
            "/inlumen/inputs/input_manifest.json",
            next(
                item["value"]
                for item in workflow["spec"]["templates"][1]["container"]["env"]
                if item["name"] == "INLUMEN_INPUT_MANIFEST"
            ),
        )

    def test_codegen_workflow_yaml_is_deterministic(self):
        yaml_text = build_argo_workflow_yaml(self.graph(), codegen_payload())
        self.assertIn('generateName: "inlumen-codegen-"', yaml_text)
        self.assertIn("artifactRepositoryRef:", yaml_text)
        self.assertIn("INLUMEN_OUTPUT_MANIFEST", yaml_text)

    def test_dagster_project_files_use_persisted_scripts_and_graph_dependencies(self):
        files = build_dagster_project_files(self.graph(), codegen_payload())
        by_path = {item["path"]: item["content"] for item in files}
        self.assertIn("dagster_project/pyproject.toml", by_path)
        self.assertIn("dagster_project/Dockerfile", by_path)
        self.assertIn("dagster_project/.dagster_home/dagster.yaml", by_path)
        pyproject = by_path["dagster_project/pyproject.toml"]
        self.assertIn("dagster==1.13.12", pyproject)
        self.assertIn("dagster-webserver==1.13.12", pyproject)
        self.assertIn("dagster-postgres==0.29.12", pyproject)
        self.assertIn('"pandas"', pyproject)
        self.assertIn('"numpy"', pyproject)
        self.assertNotIn("-r extra.txt", pyproject)
        self.assertEqual(1, pyproject.count('"pandas"'))
        self.assertIn("dagster_project/src/inlumen_dagster_project/components/shell_command.py", by_path)
        self.assertEqual(
            "print('ingest')\n",
            by_path["dagster_project/src/inlumen_dagster_project/scripts/node_1_ingestion/main.py"],
        )
        self.assertIn(
            'upstream_assets:\n    - "node_1_ingestion"',
            by_path["dagster_project/src/inlumen_dagster_project/defs/node_2_preprocessing/defs.yaml"],
        )
        self.assertIn('"inputs"', by_path["dagster_project/storage/inputs/input_manifest.json"])
        self.assertIn('"filename": "vital_signs_short.csv"', by_path["dagster_project/storage/inputs/input_manifest.json"])
        self.assertIn('"path": "storage/inputs/vital_signs_short.csv"', by_path["dagster_project/storage/inputs/input_manifest.json"])
        self.assertIn('"kind": "table"', by_path["dagster_project/storage/inputs/input_manifest.json"])
        shell_command = by_path["dagster_project/src/inlumen_dagster_project/components/shell_command.py"]
        self.assertIn("_prepare_input_manifest", shell_command)
        self.assertIn("import sys", shell_command)
        self.assertIn("project_root.parent / path", shell_command)
        self.assertIn('normalized["kind"] = inferred_kind', shell_command)
        self.assertIn('return "binary", normalized_format', shell_command)
        self.assertIn("subprocess.Popen(", shell_command)
        self.assertIn("stderr=subprocess.STDOUT", shell_command)
        self.assertIn("output_queue.get(timeout=15.0)", shell_command)
        self.assertIn("is still running", shell_command)
        self.assertNotIn("capture_output=True", shell_command)
        self.assertNotIn("PipesSubprocessClient", shell_command)
        self.assertIn("dagster_project/requirements.txt", by_path)
        self.assertIn("dagster==1.13.12", by_path["dagster_project/requirements.txt"])
        self.assertIn(
            "COPY requirements.txt /tmp/inlumen-requirements.txt",
            by_path["dagster_project/Dockerfile"],
        )
        self.assertIn(
            "download.pytorch.org/whl/cpu",
            by_path["dagster_project/Dockerfile"],
        )
        self.assertNotIn("find /app", by_path["dagster_project/Dockerfile"])
        self.assertIn("/app/.dagster_home", by_path["dagster_project/Dockerfile"])

    def test_canonical_deployment_bundle_has_runnable_layout(self):
        bundle = build_deployment_bundle_files(
            self.graph(),
            codegen_payload(),
            targets={"argo": True, "dagster": True},
        )
        by_path = {item["path"]: item["content"] for item in bundle["files"]}
        self.assertIn("README.md", by_path)
        self.assertIn("bundle-manifest.json", by_path)
        self.assertIn("inputs/input_manifest.json", by_path)
        self.assertIn("inputs/vital_signs_short.csv", by_path)
        self.assertIn("inputs/sample.wav", by_path)
        self.assertIn("nodes/node-1-ingestion/main.py", by_path)
        self.assertIn("nodes/node-1-ingestion/Dockerfile.1", by_path)
        self.assertIn("outputs/node-2-preprocessing/.gitkeep", by_path)
        self.assertIn("argo/workflow.yaml", by_path)
        self.assertIn("dagster/pyproject.toml", by_path)
        self.assertIn("dagster/requirements.txt", by_path)
        self.assertIn("dagster/.dagster_home/dagster.yaml", by_path)
        self.assertIn("dagster-webserver==1.13.12", by_path["dagster/pyproject.toml"])
        self.assertIn("dagster-postgres==0.29.12", by_path["dagster/pyproject.toml"])
        self.assertIn('"pandas"', by_path["dagster/pyproject.toml"])
        self.assertIn('"numpy"', by_path["dagster/pyproject.toml"])
        self.assertIn("dagster/Dockerfile", by_path)
        self.assertIn("dagster/docker-compose.yml", by_path)
        self.assertIn("docker-compose.yml", by_path)
        self.assertIn("docker compose up", by_path["README.md"])
        self.assertIn("http://localhost:3000", by_path["README.md"])
        self.assertNotIn("image: inlumen-generated-dagster:local", by_path["docker-compose.yml"])
        self.assertNotIn(
            "image: inlumen-generated-dagster:local",
            by_path["dagster/docker-compose.yml"],
        )
        self.assertIn("inputs/", by_path["dagster/README.md"])
        self.assertNotIn("storage/inputs/", by_path["dagster/README.md"])
        self.assertIn("COPY nodes /workspace/nodes", by_path["dagster/Dockerfile"])
        self.assertIn(
            "COPY dagster/requirements.txt /tmp/inlumen-requirements.txt",
            by_path["dagster/Dockerfile"],
        )
        self.assertIn(
            "--mount=type=cache,target=/root/.cache/pip",
            by_path["dagster/Dockerfile"],
        )
        self.assertNotIn("find /workspace/nodes", by_path["dagster/Dockerfile"])
        self.assertIn("INLUMEN_ACCELERATOR", by_path["docker-compose.yml"])
        self.assertIn("inlumen_model_cache", by_path["docker-compose.yml"])
        self.assertIn(
            'name: "${INLUMEN_MODEL_CACHE_VOLUME:-inlumen_model_cache}"',
            by_path["docker-compose.yml"],
        )
        self.assertIn('HF_TOKEN: "${HF_TOKEN:-}"', by_path["docker-compose.yml"])
        self.assertIn("dagster-postgres:", by_path["docker-compose.yml"])
        self.assertIn("dagster-webserver:", by_path["docker-compose.yml"])
        self.assertIn("dagster-daemon:", by_path["docker-compose.yml"])
        self.assertIn("dagster_postgres_data:", by_path["docker-compose.yml"])
        self.assertIn(
            "dagster_postgres.run_storage",
            by_path["dagster/.dagster_home/dagster.yaml"],
        )
        self.assertIn(
            'INLUMEN_ASR_CPU_THREADS: "${INLUMEN_ASR_CPU_THREADS:-2}"',
            by_path["docker-compose.yml"],
        )
        self.assertIn(
            'INLUMEN_ASR_PROFILE: "${INLUMEN_ASR_PROFILE:-auto}"',
            by_path["docker-compose.yml"],
        )
        self.assertIn("healthcheck:", by_path["docker-compose.yml"])
        self.assertIn(
            "@dg.asset_check",
            by_path[
                "dagster/src/inlumen_dagster_project/components/shell_command.py"
            ],
        )
        self.assertIn("/workspace/dagster/.dagster_home", by_path["dagster/Dockerfile"])
        self.assertIn("script_path: \"../nodes/node-1-ingestion/main.py\"", by_path["dagster/src/inlumen_dagster_project/defs/node_1_ingestion/defs.yaml"])
        self.assertIn('"path": "inputs/vital_signs_short.csv"', by_path["inputs/input_manifest.json"])
        self.assertIn('"kind": "table"', by_path["inputs/input_manifest.json"])
        input_manifest = json.loads(by_path["inputs/input_manifest.json"])
        inputs_by_filename = {
            entry["filename"]: entry for entry in input_manifest["inputs"]
        }
        self.assertEqual("binary", inputs_by_filename["sample.wav"]["kind"])
        self.assertEqual("wav", inputs_by_filename["sample.wav"]["format"])
        binary_file = next(
            item for item in bundle["files"] if item["path"] == "inputs/sample.wav"
        )
        self.assertEqual("base64", binary_file["content_encoding"])
        self.assertEqual(
            b"RIFF\xff\x00\x80WAVEfmt \x00data",
            base64.b64decode(binary_file["content"]),
        )
        self.assertEqual("README.md", bundle["manifest"]["readme"])

    def test_explicit_multimodal_inputs_are_separate_from_runtime_files(self):
        payload = codegen_payload()
        input_filenames = {"vital_signs_short.csv", "sample.wav"}
        source_files = [
            item
            for item in payload["deployment_files"]
            if item["filename"] in input_filenames
        ]
        payload["deployment_files"] = [
            item
            for item in payload["deployment_files"]
            if item["filename"] not in input_filenames
        ]
        payload["input_files"] = []
        for item in source_files:
            encoded = base64.b64decode(item["content"]) if item.get(
                "content_encoding"
            ) == "base64" else item["content"].encode("utf-8")
            payload["input_files"].append(
                {
                    **item,
                    "role": "input",
                    "content_encoding": item.get("content_encoding") or "utf-8",
                    "size_bytes": len(encoded),
                    "sha256": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
                }
            )

        bundle = build_deployment_bundle_files(
            self.graph(),
            payload,
            targets={"argo": False, "dagster": True},
        )
        by_path = {item["path"]: item for item in bundle["files"]}

        self.assertIn("inputs/vital_signs_short.csv", by_path)
        self.assertIn("inputs/sample.wav", by_path)
        self.assertNotIn("nodes/node-1-ingestion/sample.wav", by_path)
        self.assertEqual(2, bundle["manifest"]["inputs"]["sample_file_count"])
        self.assertIn(
            "./inputs:/workspace/inputs:ro",
            by_path["docker-compose.yml"]["content"],
        )

    def test_missing_required_root_input_is_rejected_before_export(self):
        payload = codegen_payload()
        payload["input_files"] = []

        with self.assertRaises(DeploymentArtifactValidationError) as raised:
            build_deployment_bundle_files(
                self.graph(),
                payload,
                targets={"argo": False, "dagster": True},
            )

        self.assertTrue(
            any(
                "requires input vital_signs_short.csv" in error
                for error in raised.exception.errors
            )
        )

    def test_bundle_input_manifest_classifies_supported_modalities(self):
        payload = deepcopy(codegen_payload())
        additional_inputs = {
            "document.pdf": "binary",
            "photo.png": "image",
            "notes.txt": "text",
            "events.json": "json",
            "records.parquet": "table",
        }
        for filename in additional_inputs:
            payload["deployment_files"].append(
                {
                    "path": f"nodes/1/{filename}",
                    "filename": filename,
                    "flow_id": "1",
                    "content": "sample",
                }
            )

        bundle = build_deployment_bundle_files(
            self.graph(),
            payload,
            targets={"argo": False, "dagster": True},
        )
        manifest_file = next(
            item
            for item in bundle["files"]
            if item["path"] == "inputs/input_manifest.json"
        )
        entries = {
            entry["filename"]: entry
            for entry in json.loads(manifest_file["content"])["inputs"]
        }

        expected = {
            "vital_signs_short.csv": "table",
            "sample.wav": "binary",
            **additional_inputs,
        }
        self.assertEqual(
            expected,
            {filename: entries[filename]["kind"] for filename in expected},
        )


if __name__ == "__main__":
    unittest.main()
