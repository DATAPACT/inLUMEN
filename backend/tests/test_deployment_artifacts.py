import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


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
        self.assertIn(
            "from dagster import Definitions, in_process_executor, job, op",
            python_text,
        )
        self.assertIn(
            '@job(name="inlumen_pipeline", executor_def=in_process_executor)',
            python_text,
        )
        self.assertIn(
            "process_data_result = process_data(upstream_retrieve_data=retrieve_data_result)",
            python_text,
        )
        self.assertIn('@op(name="retrieve_data"', python_text)
        self.assertIn(
            'script_invocations = [{"script": "process.py", "interpreter": "python", "args": []}]',
            python_text,
        )
        self.assertIn(
            "[sys.executable, str(script_path), *script_args]",
            python_text,
        )
        self.assertIn("python -m pip install -r requirements.txt", python_text)
        self.assertIn("defs = Definitions(jobs=[inlumen_pipeline])", python_text)
        self.assertIn("executor_def=in_process_executor", python_text)
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
                    ".dagster/",
                    ".dagster/.gitkeep",
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
            self.assertIn(
                'script_invocations = [{"script": "retrieve.py", "interpreter": "python", "args": []}]',
                definitions,
            )
            self.assertIn('SCRIPTS_DIR = BASE_DIR / "scripts"', definitions)
            self.assertIn('OUTPUT_DIR = Path(os.getenv("INLUMEN_OUTPUT_DIR"', definitions)
            self.assertIn("step_output_dir = OUTPUT_DIR / context.op.name", definitions)
            self.assertIn('script_env["INLUMEN_DATA_DIR"]', definitions)
            self.assertIn('BASE_DIR / "_dagster_script_runner.py"', definitions)
            self.assertIn('legacy_output_dir = DATA_DIR / "output"', definitions)
            self.assertIn("produced_files = collect_project_outputs", definitions)
            self.assertIn('f"{script_path.stem}.outputs.json"', definitions)
            self.assertIn("pandas==2.3.0", requirements)
            self.assertIn("dagster\n", requirements)
            self.assertIn("dagster-webserver\n", requirements)
            self.assertIn("name: inlumen-dagster", compose)
            self.assertIn("container_name: inlumen-dagster", compose)
            self.assertIn("- .:/app", compose)
            self.assertIn("dagster_home:", compose)
            self.assertIn("- dagster_home:/app/.dagster", compose)
            self.assertIn("DAGSTER_HOME: /app/.dagster", compose)
            self.assertNotIn("./output:/app/output", compose)
            self.assertNotIn("./data:/app/data", compose)
            self.assertIn("DAGSTER_HOME=/app/.dagster", dockerfile)
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

    def test_bundle_runner_calls_main_function_with_clean_argv(self):
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Writer",
                        "files": ["writer.py"],
                    },
                },
            ],
            "edges": [],
        }
        script = b"""import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/main-result.txt")
    args = parser.parse_args()
    Path(args.output).write_text("MAIN")
"""
        bundle = build_dagster_bundle_zip(
            graph,
            [{"step_id": "1", "filename": "writer.py", "content": script}],
        )

        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                archive.extractall(directory)
            subprocess.run(
                [
                    sys.executable,
                    os.path.join(directory, "_dagster_script_runner.py"),
                    os.path.join(directory, "scripts", "writer.py"),
                ],
                cwd=directory,
                check=True,
            )
            self.assertEqual(
                "MAIN",
                Path(directory, "data", "main-result.txt").read_text(),
            )

    def test_manifest_controls_file_placement_and_script_arguments(self):
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Ingestion",
                        "files": ["ingest.py", "patients.csv", "requirements.txt"],
                    },
                },
            ],
            "edges": [],
        }
        attached_files = [
            {
                "step_id": "1",
                "filename": "ingest.py",
                "content": b"print('ingest')\n",
            },
            {
                "step_id": "1",
                "filename": "patients.csv",
                "content": b"id\n1\n",
            },
            {
                "step_id": "1",
                "filename": "requirements.txt",
                "content": b"pandas\n",
            },
        ]
        manifest = {
            "files": [
                {
                    "step_id": "1",
                    "source_filename": "ingest.py",
                    "role": "script",
                    "destination": "scripts/ingest.py",
                    "args": ["--input", "data/patients.csv"],
                },
                {
                    "step_id": "1",
                    "source_filename": "patients.csv",
                    "role": "input",
                    "destination": "data/patients.csv",
                    "args": [],
                },
                {
                    "step_id": "1",
                    "source_filename": "requirements.txt",
                    "role": "requirements",
                    "destination": "requirements.txt",
                    "args": [],
                },
            ],
            "checks": ["Script input path matches packaged data path"],
        }

        bundle = build_dagster_bundle_zip(
            graph,
            attached_files,
            packaging_manifest=manifest,
        )

        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn("data/patients.csv", archive.namelist())
            self.assertNotIn("data/ingestion/patients.csv", archive.namelist())
            self.assertEqual(
                manifest,
                json.loads(archive.read("packaging_manifest.json")),
            )
            definitions = archive.read("definitions.py").decode()
            self.assertIn(
                '"args": ["--input", "data/patients.csv"]',
                definitions,
            )

    def test_generated_definitions_persist_direct_and_legacy_outputs(self):
        graph = {
            "nodes": [
                {
                    "id": "1",
                    "data": {
                        "label": "Writer",
                        "files": ["writer.py"],
                    },
                },
            ],
            "edges": [],
        }
        script = b"""import os
from pathlib import Path

Path(os.environ["INLUMEN_OUTPUT_DIR"], "direct.txt").write_text("direct")
Path("result.txt").write_text("root")
Path("data", "changed.txt").write_text("data")
"""
        bundle = build_dagster_bundle_zip(
            graph,
            [{"step_id": "1", "filename": "writer.py", "content": script}],
        )

        fake_dagster = types.ModuleType("dagster")
        fake_dagster.op = lambda **_kwargs: lambda function: function
        fake_dagster.job = lambda **_kwargs: lambda function: function
        fake_dagster.Definitions = lambda **kwargs: kwargs
        fake_dagster.in_process_executor = object()
        previous_dagster = sys.modules.get("dagster")
        sys.modules["dagster"] = fake_dagster
        try:
            with tempfile.TemporaryDirectory() as directory:
                with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                    archive.extractall(directory)
                namespace = runpy.run_path(os.path.join(directory, "definitions.py"))
                logger = types.SimpleNamespace(
                    info=lambda *_args: None,
                    warning=lambda *_args: None,
                )
                context = types.SimpleNamespace(
                    op=types.SimpleNamespace(name="writer"),
                    log=logger,
                )

                namespace["run_step_scripts"](context, ["writer.py"])

                output_dir = Path(directory, "output", "writer")
                self.assertEqual("direct", (output_dir / "direct.txt").read_text())
                self.assertEqual(
                    "root",
                    (output_dir / "files" / "result.txt").read_text(),
                )
                self.assertEqual(
                    "data",
                    (output_dir / "files" / "data" / "changed.txt").read_text(),
                )
                manifest = json.loads(
                    (output_dir / "writer.outputs.json").read_text()
                )
                self.assertIn("direct.txt", manifest["files"])
                self.assertIn("result.txt", manifest["project_files"])
                self.assertIn("data/changed.txt", manifest["project_files"])
        finally:
            if previous_dagster is None:
                sys.modules.pop("dagster", None)
            else:
                sys.modules["dagster"] = previous_dagster

    def test_generated_file_scanner_ignores_disappearing_dagster_runtime_files(self):
        graph = {
            "nodes": [{"id": "1", "data": {"label": "Writer"}}],
            "edges": [],
        }
        bundle = build_dagster_bundle_zip(graph, [])
        fake_dagster = types.ModuleType("dagster")
        fake_dagster.op = lambda **_kwargs: lambda function: function
        fake_dagster.job = lambda **_kwargs: lambda function: function
        fake_dagster.Definitions = lambda **kwargs: kwargs
        fake_dagster.in_process_executor = object()
        previous_dagster = sys.modules.get("dagster")
        sys.modules["dagster"] = fake_dagster
        try:
            with tempfile.TemporaryDirectory() as directory:
                with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                    archive.extractall(directory)
                transient_dir = Path(
                    directory,
                    ".tmp_dagster_home_test",
                    "history",
                    "runs",
                )
                transient_dir.mkdir(parents=True)
                transient_file = transient_dir / "run.db-wal"
                transient_file.write_text("temporary")
                namespace = runpy.run_path(os.path.join(directory, "definitions.py"))
                original_stat = Path.stat

                def racing_stat(path, *args, **kwargs):
                    if path == transient_file:
                        raise FileNotFoundError(path)
                    return original_stat(path, *args, **kwargs)

                with patch.object(Path, "stat", racing_stat):
                    state = namespace["project_file_state"]()

                self.assertNotIn(
                    ".tmp_dagster_home_test/history/runs/run.db-wal",
                    state,
                )
        finally:
            if previous_dagster is None:
                sys.modules.pop("dagster", None)
            else:
                sys.modules["dagster"] = previous_dagster

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
