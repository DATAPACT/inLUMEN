import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment_artifacts import (  # noqa: E402
    DeploymentArtifactValidationError,
    build_argo_workflow_object,
    build_argo_workflow_yaml,
    build_dagster_bundle_zip,
    build_dagster_definitions_py,
    build_dockerfile_artifacts,
    validate_argo_workflow_object,
    validate_dagster_definitions_py,
    validate_dockerfile_artifacts,
)


class DeploymentArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Retrieve data",
                        "type": "input",
                        "files": ["retrieve.sh", "requirements.txt"],
                    },
                },
                {
                    "id": "2",
                    "data": {
                        "label": "Process data",
                        "type": "action",
                        "files": ["process.py"],
                    },
                },
                {
                    "id": "3",
                    "data": {
                        "label": "Notify",
                        "type": "output",
                    },
                },
            ],
            "edges": [
                {"source": "1", "target": "2"},
                {"source": "2", "target": "3"},
            ],
        }

    def test_builds_one_valid_dockerfile_per_pipeline_step(self):
        artifacts = build_dockerfile_artifacts(self.graph)

        dockerfiles = artifacts["dockerfiles"]
        self.assertEqual(["1", "2", "3"], [item["flow_id"] for item in dockerfiles])
        self.assertEqual(
            ["Dockerfile.1", "Dockerfile.2", "Dockerfile.3"],
            [item["dockerfile_filename"] for item in dockerfiles],
        )
        validate_dockerfile_artifacts(
            dockerfiles,
            expected_step_ids=["1", "2", "3"],
            steps=[
                {"flow_id": "1", "files": [{"filename": "retrieve.sh"}, {"filename": "requirements.txt"}]},
                {"flow_id": "2", "files": [{"filename": "process.py"}]},
                {"flow_id": "3", "files": []},
            ],
        )
        self.assertIn("RUN pip install --no-cache-dir -r requirements.txt", dockerfiles[0]["content"])
        self.assertIn('RUN find /app -type f -name "*.sh"', dockerfiles[0]["content"])

    def test_dockerfile_guardrail_rejects_bad_format(self):
        with self.assertRaises(DeploymentArtifactValidationError) as ctx:
            validate_dockerfile_artifacts(
                [{"dockerfile_filename": "Dockerfile.1", "content": "WORKDIR /app\nCMD [\"true\"]\n"}],
                expected_step_ids=["1"],
            )

        self.assertIn("must start with a FROM instruction", str(ctx.exception))

    def test_builds_valid_argo_workflow_dag_from_graph_edges(self):
        dockerfiles = build_dockerfile_artifacts(self.graph)
        workflow = build_argo_workflow_object(self.graph, dockerfiles)

        validate_argo_workflow_object(workflow, expected_step_ids=["1", "2", "3"])
        tasks = workflow["spec"]["templates"][0]["dag"]["tasks"]
        self.assertEqual("step-1", tasks[0]["name"])
        self.assertEqual(["step-1"], tasks[1]["dependencies"])
        self.assertEqual(["step-2"], tasks[2]["dependencies"])
        self.assertEqual("inlumen/step-2:latest", workflow["spec"]["templates"][2]["container"]["image"])

    def test_argo_yaml_output_is_plain_yaml_not_markdown(self):
        dockerfiles = build_dockerfile_artifacts(self.graph)
        yaml_text = build_argo_workflow_yaml(self.graph, dockerfiles)

        self.assertIn('apiVersion: "argoproj.io/v1alpha1"', yaml_text)
        self.assertIn('kind: "Workflow"', yaml_text)
        self.assertIn('template: "step-3"', yaml_text)
        self.assertNotIn("```", yaml_text)

    def test_argo_guardrail_requires_dockerfile_for_each_step(self):
        dockerfiles = build_dockerfile_artifacts(self.graph)
        dockerfiles["dockerfiles"] = dockerfiles["dockerfiles"][:2]

        with self.assertRaises(DeploymentArtifactValidationError) as ctx:
            build_argo_workflow_object(self.graph, dockerfiles)

        self.assertIn("missing Dockerfile for step id '3'", str(ctx.exception))

    def test_argo_guardrail_rejects_cyclic_pipeline(self):
        graph = {
            "nodes": self.graph["nodes"][:2],
            "edges": [
                {"source": "1", "target": "2"},
                {"source": "2", "target": "1"},
            ],
        }
        dockerfiles = build_dockerfile_artifacts(graph)

        with self.assertRaises(DeploymentArtifactValidationError) as ctx:
            build_argo_workflow_object(graph, dockerfiles)

        self.assertIn("pipeline graph contains a cycle", str(ctx.exception))

    def test_builds_valid_dagster_definitions_from_graph_edges(self):
        python_text = build_dagster_definitions_py(self.graph)

        validate_dagster_definitions_py(python_text, expected_step_ids=["1", "2", "3"])
        self.assertIn("from dagster import Definitions, job, op", python_text)
        self.assertIn('@job(name="inlumen_pipeline")', python_text)
        self.assertIn(
            "process_data_result = process_data(upstream_retrieve_data=retrieve_data_result)",
            python_text,
        )
        self.assertIn('@op(name="retrieve_data"', python_text)
        self.assertIn('scripts = ["process.py"]', python_text)
        self.assertIn("[sys.executable, str(script_path)]", python_text)
        self.assertIn("python -m pip install -r requirements.txt", python_text)
        self.assertIn("defs = Definitions(jobs=[inlumen_pipeline])", python_text)
        self.assertNotIn("```", python_text)

    def test_dagster_step_names_are_unique_when_labels_repeat(self):
        graph = {
            "nodes": [
                {"id": "one", "data": {"label": "Process data", "files": ["first.py"]}},
                {"id": "two", "data": {"label": "Process data", "files": ["second.py"]}},
            ],
            "edges": [{"source": "one", "target": "two"}],
        }

        python_text = build_dagster_definitions_py(graph)

        self.assertIn('@op(name="process_data"', python_text)
        self.assertIn('@op(name="process_data_2"', python_text)
        self.assertIn(
            "process_data_2_result = process_data_2("
            "upstream_process_data=process_data_result)",
            python_text,
        )

    def test_dagster_prefers_step_name_and_script_name_over_generic_label(self):
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "name": "Load patient records",
                        "label": "step_1",
                        "files": ["load.py"],
                    },
                },
                {
                    "id": "2",
                    "data": {
                        "label": "step_2",
                        "files": ["calculate_risk.py"],
                    },
                },
            ],
            "edges": [{"source": "1", "target": "2"}],
        }

        python_text = build_dagster_definitions_py(graph)

        self.assertIn('@op(name="load_patient_records"', python_text)
        self.assertIn('@op(name="calculate_risk"', python_text)
        self.assertNotIn('@op(name="step_1"', python_text)
        self.assertNotIn('@op(name="step_2"', python_text)

    def test_builds_runnable_dagster_zip_with_scripts_data_and_requirements(self):
        attached_files = [
            {"step_id": "1", "filename": "retrieve.py", "content": b"print('retrieve')\n"},
            {"step_id": "1", "filename": "patients.csv", "content": b"id,name\n1,Ada\n"},
            {"step_id": "2", "filename": "process.py", "content": b"print('process')\n"},
            {"step_id": "2", "filename": "requirements.txt", "content": b"pandas==2.3.0\n"},
        ]

        bundle = build_dagster_bundle_zip(self.graph, attached_files)

        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(
                {
                    ".dockerignore",
                    "_dagster_script_runner.py",
                    "Dockerfile",
                    "README.md",
                    "data/",
                    "data/patients.csv",
                    "data/retrieve_data/patients.csv",
                    "definitions.py",
                    "docker-compose.yml",
                    "output/",
                    "output/.gitkeep",
                    "requirements.txt",
                    "scripts/",
                    "scripts/process.py",
                    "scripts/retrieve.py",
                },
                set(archive.namelist()),
            )
            definitions = archive.read("definitions.py").decode()
            requirements = archive.read("requirements.txt").decode()
            compose = archive.read("docker-compose.yml").decode()
            dockerfile = archive.read("Dockerfile").decode()
            self.assertIn('@op(name="retrieve_data"', definitions)
            self.assertIn('scripts = ["retrieve.py"]', definitions)
            self.assertIn('SCRIPTS_DIR = BASE_DIR / "scripts"', definitions)
            self.assertIn('OUTPUT_DIR = Path(os.getenv("INLUMEN_OUTPUT_DIR"', definitions)
            self.assertIn("step_output_dir = OUTPUT_DIR / context.op.name", definitions)
            self.assertIn('script_env["INLUMEN_DATA_DIR"]', definitions)
            self.assertIn('BASE_DIR / "_dagster_script_runner.py"', definitions)
            self.assertIn('legacy_output_dir = DATA_DIR / "output"', definitions)
            self.assertIn("pandas==2.3.0", requirements)
            self.assertIn("dagster\n", requirements)
            self.assertIn("dagster-webserver\n", requirements)
            self.assertIn("./output:/app/output", compose)
            self.assertIn("./data:/app/data", compose)
            self.assertIn("INLUMEN_OUTPUT_DIR=/app/output", dockerfile)

    def test_bundle_runner_calls_run_function_without_main_block(self):
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "step_1",
                        "description": "Create a standalone result.",
                        "files": ["step_1_writer.py", "input.txt"],
                    },
                },
            ],
            "edges": [],
        }
        script = b"""from pathlib import Path

def run():
    value = Path("data/input.txt").read_text()
    target = Path("data/output/result.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value.upper())
"""
        bundle = build_dagster_bundle_zip(
            graph,
            [
                {"step_id": "1", "filename": "step_1_writer.py", "content": script},
                {"step_id": "1", "filename": "input.txt", "content": b"standalone"},
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                archive.extractall(directory)
            subprocess.run(
                [
                    sys.executable,
                    os.path.join(directory, "_dagster_script_runner.py"),
                    os.path.join(directory, "scripts", "step_1_writer.py"),
                ],
                cwd=directory,
                check=True,
            )
            result = Path(directory, "data", "output", "result.txt").read_text()
            self.assertEqual("STANDALONE", result)

    def test_dagster_guardrail_rejects_cyclic_pipeline(self):
        graph = {
            "nodes": self.graph["nodes"][:2],
            "edges": [
                {"source": "1", "target": "2"},
                {"source": "2", "target": "1"},
            ],
        }

        with self.assertRaises(DeploymentArtifactValidationError) as ctx:
            build_dagster_definitions_py(graph)

        self.assertIn("pipeline graph contains a cycle", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
