"""Regression coverage for missing Source data and producer handoff failures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment_agents import _managed_adapter_runtime
from deployment_artifacts import (
    _ARGO_PORT_RUNNER,
    DeploymentArtifactValidationError,
    build_dagster_project_files,
    build_deployment_bundle_files,
)
from filesystem_runtime import (
    filesystem_shell_component_source,
    normalize_single_output_port,
    stage_input_bindings,
    validate_output_ports,
)


def source_payload(template="Custom", inputs=()):
    step = {
        "flow_id": "1", "label": "Uploaded data", "type": "source",
        "template": template,
        "ports": {"inputs": [], "outputs": [{"id": "data", "required": True}]},
    }
    artifact, dockerfile = _managed_adapter_runtime(step)
    graph = {"nodes": [{"id": "1", "data": {
        "label": step["label"], "type": "source", "template_label": template,
    }}], "edges": []}
    payload = {"runtime_artifacts": [artifact], "dockerfiles": [dockerfile], "input_files": []}
    for filename, body in inputs:
        payload["input_files"].append({
            "flow_id": "1", "filename": filename, "content": body,
            "size_bytes": len(body.encode()),
            "sha256": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        })
    return graph, payload


class PipelineHandoffTest(unittest.TestCase):
    def test_file_backed_sources_require_data_before_snapshot_for_both_engines(self):
        for template in ("Custom", "File", "Folder", "User Upload"):
            graph, payload = source_payload(template)
            for targets in ({"argo": True}, {"dagster": True, "argo": False}):
                with self.subTest(template=template, targets=targets):
                    with self.assertRaisesRegex(DeploymentArtifactValidationError, "Uploaded data.*has no input files"):
                        build_deployment_bundle_files(graph, payload, targets=targets)
            with self.assertRaisesRegex(DeploymentArtifactValidationError, "Attach data files"):
                build_dagster_project_files(graph, payload)

    def test_remote_sources_do_not_require_local_attachments(self):
        for template in ("Database", "REST API", "Object Storage"):
            with self.subTest(template=template):
                graph, payload = source_payload(template)
                bundle = build_deployment_bundle_files(graph, payload, targets={"dagster": True, "argo": False})
                self.assertEqual(0, bundle["manifest"]["inputs"]["file_count"])

    def test_attached_custom_source_can_generate_its_own_data(self):
        graph, payload = source_payload()
        payload["runtime_artifacts"][0]["generator"] = "inlumen-attached-runtime"
        build_deployment_bundle_files(graph, payload, targets={"dagster": True, "argo": False})

    def test_input_on_one_source_does_not_satisfy_another_source(self):
        graph, payload = source_payload(inputs=[("records.csv", "x\n1\n")])
        graph["nodes"].append({"id": "2", "data": {"label": "Missing data", "type": "source", "template_label": "File"}})
        artifact, dockerfile = _managed_adapter_runtime({
            "flow_id": "2", "label": "Missing data", "type": "source", "template": "File",
            "ports": {"inputs": [], "outputs": [{"id": "data", "required": True}]},
        })
        payload["runtime_artifacts"].append(artifact)
        payload["dockerfiles"].append(dockerfile)
        # An explicit graph edge avoids the legacy implicit-chain fallback.
        graph["nodes"].append({"id": "3", "data": {"label": "Sink", "type": "destination"}})
        artifact, dockerfile = _managed_adapter_runtime({"flow_id": "3", "type": "destination", "template": "Custom"})
        payload["runtime_artifacts"].append(artifact)
        payload["dockerfiles"].append(dockerfile)
        graph["edges"] = [{"source": "1", "target": "3"}, {"source": "2", "target": "3"}]
        with self.assertRaisesRegex(DeploymentArtifactValidationError, "Missing data.*has no input files"):
            build_deployment_bundle_files(graph, payload, targets={"dagster": True, "argo": False})

    def test_source_adapter_rejects_empty_input_at_runtime(self):
        _, payload = source_payload()
        source = next(f["content"] for f in payload["runtime_artifacts"][0]["files"] if f["filename"] == "main.py")
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, "-c", source], env={
                **os.environ, "PIPELINE_INPUT_DIR": tmp + "/missing", "PIPELINE_OUTPUT_DIR": tmp + "/output",
            }, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Source 'Uploaded data' has no input files", result.stderr)

    def test_source_handoff_preserves_arbitrary_files_and_nested_paths(self):
        fixtures = {
            "recording.wav": b"RIFF\x00\xffWAVE", "rows.csv": b"x\n1\n", "image.png": b"\x89PNG\xff",
            "nested/document.pdf": b"%PDF", "data/unknown.custom": b"\x00\x01", "empty.txt": b"",
        }
        _, payload = source_payload()
        source = next(f["content"] for f in payload["runtime_artifacts"][0]["files"] if f["filename"] == "main.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, body in fixtures.items():
                path = root / "input" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            result = subprocess.run([sys.executable, "-c", source], env={
                **os.environ, "PIPELINE_INPUT_DIR": str(root / "input"), "PIPELINE_OUTPUT_DIR": str(root / "output"),
            }, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            normalize_single_output_port(root / "output", ["data"])
            validate_output_ports(root / "output", ["data"])
            for consumer in ("task-a", "task-b"):
                staged = stage_input_bindings([{"source_dir": str(root / "output"), "source_port": "data"}], root / consumer)
                self.assertEqual(fixtures, {p.relative_to(staged).as_posix(): p.read_bytes() for p in staged.rglob("*") if p.is_file()})

    def test_local_and_exported_validation_reject_missing_empty_and_metadata_only_outputs(self):
        namespace = {}
        exec(filesystem_shell_component_source().replace("import dagster as dg", "").split("\nclass ShellCommand", 1)[0], namespace)
        for validator in (validate_output_ports, namespace["_validate_output_ports"]):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for state in ("missing", "empty", "metadata"):
                    with self.subTest(validator=validator.__name__, state=state):
                        if state != "missing":
                            (root / "data").mkdir(exist_ok=True)
                        if state == "metadata":
                            (root / "data" / "output_manifest.json").write_text("{}")
                        with self.assertRaisesRegex(RuntimeError, "Required output port 'data' contains no artifacts"):
                            validator(root, ["data"])
                # Optional outputs and destinations are allowed to have no files.
                validator(root, [])
                (root / "data" / "empty.csv").write_bytes(b"")
                validator(root, ["data"])

    def test_argo_checks_outputs_before_reporting_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            for required, expected_code in ((["data"], 1), ([], 0)):
                result = subprocess.run([
                    sys.executable, "-c", _ARGO_PORT_RUNNER,
                    json.dumps([sys.executable, "-c", "pass"]), '["data"]', '[]', '[]', json.dumps(required),
                ], env={**os.environ, "PIPELINE_OUTPUT_DIR": tmp}, capture_output=True, text=True)
                self.assertEqual(expected_code, result.returncode, result.stderr)
                if required:
                    self.assertIn("Required output port 'data' contains no artifacts", result.stderr)

    def test_data_attachments_are_packaged_and_required_outputs_are_exported(self):
        graph, payload = source_payload(inputs=[("nested/records.csv", "x\n1\n")])
        bundle = build_deployment_bundle_files(graph, payload, targets={"dagster": True, "argo": True})
        by_path = {f["path"]: f["content"] for f in bundle["files"]}
        self.assertTrue(any(path.endswith("nested/records.csv") for path in by_path))
        defs = next(body for path, body in by_path.items() if path.endswith("defs.yaml"))
        self.assertIn('required_output_ports:\n    - "data"', defs)


if __name__ == "__main__":
    unittest.main()
