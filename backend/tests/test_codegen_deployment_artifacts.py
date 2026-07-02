import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment_artifacts import build_argo_workflow_object, build_argo_workflow_yaml, build_dagster_project_files


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
        self.assertIn("dagster_project/src/inlumen_dagster_project/components/shell_command.py", by_path)
        self.assertEqual(
            "print('ingest')\n",
            by_path["dagster_project/src/inlumen_dagster_project/scripts/node_1_ingestion/main.py"],
        )
        self.assertIn(
            'upstream_assets:\n    - "node_1_ingestion"',
            by_path["dagster_project/src/inlumen_dagster_project/defs/node_2_preprocessing/defs.yaml"],
        )
        self.assertIn('"filename": "vital_signs_short.csv"', by_path["dagster_project/storage/inputs/input_manifest.json"])


if __name__ == "__main__":
    unittest.main()
