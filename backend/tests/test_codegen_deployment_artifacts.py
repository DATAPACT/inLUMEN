import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment_artifacts import (
    _dagster_node_runner_source,
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


def codegen_payload():
    content = dockerfile_content()
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
            {"path": "nodes/1/requirements.txt", "filename": "requirements.txt", "flow_id": "1", "content": "pandas\n"},
            {"path": "nodes/1/node-manifest.json", "filename": "node-manifest.json", "flow_id": "1", "content": "{}\n"},
            {"path": "nodes/1/vital_signs_short.csv", "filename": "vital_signs_short.csv", "flow_id": "1", "content": "heart_rate\n72\n"},
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
        project_metadata = by_path["dagster_project/pyproject.toml"]
        self.assertIn("dagster==1.13.14", project_metadata)
        self.assertIn("dagster-webserver==1.13.14", project_metadata)
        self.assertIn("dagster-dg-cli==1.13.14", project_metadata)
        self.assertIn('build-backend = "hatchling.build"', project_metadata)
        self.assertIn(
            "dagster_project/src/inlumen_dagster_project/defs/__init__.py",
            by_path,
        )
        self.assertIn(
            "dagster_project/src/inlumen_dagster_project/components/node_runner.py",
            by_path,
        )
        self.assertIn(
            'CMD ["dg", "dev", "--host", "0.0.0.0", "--port", "3000"]',
            by_path["dagster_project/Dockerfile"],
        )
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
        self.assertIn('normalized["kind"] = "table"', shell_command)
        self.assertIn("_copy_node_workspace(source_dir, output_dir)", shell_command)
        self.assertIn("_ensure_output_manifest(", shell_command)
        self.assertIn("INLUMEN_NODE_TIMEOUT_SECONDS", shell_command)
        self.assertIn('"code 124"', shell_command)
        self.assertIn(
            "str(runner_path)",
            shell_command,
        )
        self.assertIn("cwd=str(output_dir)", shell_command)

    def test_dagster_project_uses_attached_entry_script_name_and_arguments(self):
        payload = codegen_payload()
        payload["dockerfiles"][0]["command"] = [
            "python",
            "/app/ingest_pdf.py",
            "--batch-size",
            "20",
        ]
        payload["dockerfiles"][0]["content"] = payload["dockerfiles"][0]["content"].replace(
            "/app/main.py",
            "/app/ingest_pdf.py",
        ).replace(
            '"main.py"',
            '"ingest_pdf.py"',
        )
        payload["dockerfiles"][0]["files"] = [
            "requirements.txt",
            "ingest_pdf.py",
            "node-manifest.json",
        ]
        for file_entry in payload["deployment_files"]:
            if file_entry["flow_id"] == "1" and file_entry["filename"] == "main.py":
                file_entry["filename"] = "ingest_pdf.py"
                file_entry["path"] = "nodes/1/ingest_pdf.py"
                file_entry["content"] = "print('attached ingest script')\n"

        files = build_dagster_project_files(self.graph(), payload)
        by_path = {item["path"]: item["content"] for item in files}
        defs = by_path[
            "dagster_project/src/inlumen_dagster_project/defs/node_1_ingestion/defs.yaml"
        ]
        self.assertIn(
            "dagster_project/src/inlumen_dagster_project/scripts/"
            "node_1_ingestion/ingest_pdf.py",
            by_path,
        )
        self.assertIn(
            'script_path: "src/inlumen_dagster_project/artifacts/nodes/'
            '1/ingest_pdf.py"',
            defs,
        )
        self.assertIn('    - "--batch-size"', defs)
        self.assertIn('    - "20"', defs)

    def test_dagster_project_accepts_plain_script_without_packaging_files(self):
        payload = codegen_payload()
        payload["deployment_files"] = [
            file_entry
            for file_entry in payload["deployment_files"]
            if not (
                file_entry["flow_id"] == "2"
                and file_entry["filename"] in {"requirements.txt", "node-manifest.json"}
            )
        ]

        files = build_dagster_project_files(self.graph(), payload)
        by_path = {item["path"]: item["content"] for item in files}
        defs = by_path[
            "dagster_project/src/inlumen_dagster_project/defs/node_2_preprocessing/defs.yaml"
        ]
        self.assertIn('context_path: ""', defs)
        self.assertIn(
            'script_path: "src/inlumen_dagster_project/artifacts/nodes/2/main.py"',
            defs,
        )

    def test_each_node_receives_its_own_attached_inputs(self):
        payload = codegen_payload()
        payload["deployment_files"].append(
            {
                "path": "nodes/2/lookup.json",
                "filename": "lookup.json",
                "flow_id": "2",
                "content": '{"A": 1}\n',
            }
        )

        files = build_dagster_project_files(self.graph(), payload)
        by_path = {item["path"]: item["content"] for item in files}
        defs = by_path[
            "dagster_project/src/inlumen_dagster_project/defs/node_2_preprocessing/defs.yaml"
        ]
        self.assertIn("local_input_paths:", defs)
        self.assertIn(
            '"src/inlumen_dagster_project/artifacts/nodes/2/lookup.json"',
            defs,
        )

    def test_canonical_bundle_preserves_binary_node_input_encoding(self):
        payload = codegen_payload()
        payload["deployment_files"].append(
            {
                "path": "nodes/1/input.pdf",
                "filename": "input.pdf",
                "flow_id": "1",
                "content": "JVBERi0xLjQK/wA=",
                "content_type": "application/pdf",
                "encoding": "base64",
            }
        )

        bundle = build_deployment_bundle_files(
            self.graph(),
            payload,
            targets={"argo": False, "dagster": True},
        )
        by_path = {item["path"]: item for item in bundle["files"]}
        self.assertEqual("base64", by_path["nodes/node-1-ingestion/input.pdf"]["encoding"])
        self.assertEqual("base64", by_path["inputs/input.pdf"]["encoding"])
        self.assertEqual("application/pdf", by_path["inputs/input.pdf"]["content_type"])

    def test_canonical_deployment_bundle_has_runnable_layout(self):
        bundle = build_deployment_bundle_files(
            self.graph(),
            codegen_payload(),
            targets={"argo": True, "dagster": True},
        )
        by_path = {item["path"]: item["content"] for item in bundle["files"]}
        self.assertIn("bundle-manifest.json", by_path)
        self.assertIn("inputs/input_manifest.json", by_path)
        self.assertIn("inputs/vital_signs_short.csv", by_path)
        self.assertIn("nodes/node-1-ingestion/main.py", by_path)
        self.assertIn("nodes/node-1-ingestion/Dockerfile.1", by_path)
        self.assertIn("outputs/node-2-preprocessing/.gitkeep", by_path)
        self.assertIn("argo/workflow.yaml", by_path)
        self.assertIn("dagster/pyproject.toml", by_path)
        self.assertIn("dagster/Dockerfile", by_path)
        self.assertEqual("{}\n", by_path["dagster/config/dagster.yaml"])
        self.assertIn("dagster/docker-compose.yml", by_path)
        self.assertIn("docker-compose.yml", by_path)
        self.assertIn("COPY nodes /workspace/nodes", by_path["dagster/Dockerfile"])
        self.assertIn(
            'CMD ["dg", "dev", "--host", "0.0.0.0", "--port", "3000"]',
            by_path["dagster/Dockerfile"],
        )
        self.assertIn(
            "dagster_home:/workspace/dagster/.dagster_home",
            by_path["docker-compose.yml"],
        )
        self.assertIn(
            'INLUMEN_NODE_TIMEOUT_SECONDS: "${INLUMEN_NODE_TIMEOUT_SECONDS:-300}"',
            by_path["docker-compose.yml"],
        )
        self.assertIn("script_path: \"../nodes/node-1-ingestion/main.py\"", by_path["dagster/src/inlumen_dagster_project/defs/node_1_ingestion/defs.yaml"])
        self.assertIn('"path": "inputs/vital_signs_short.csv"', by_path["inputs/input_manifest.json"])
        self.assertIn('"kind": "table"', by_path["inputs/input_manifest.json"])

    def test_node_runner_terminates_non_finite_external_script(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / "node_runner.py"
            runner.write_text(_dagster_node_runner_source(), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "1",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

        self.assertEqual(124, completed.returncode)
        self.assertIn("timed out after 1 seconds", completed.stderr)


if __name__ == "__main__":
    unittest.main()
