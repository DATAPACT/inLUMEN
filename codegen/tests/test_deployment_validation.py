import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.deployment_validation import (
    _read_run_output_files,
    repair_deployment_bundle,
    validate_dagster_project,
    validate_deployment_bundle,
)


class DagsterRuntimeDependencyValidationTest(unittest.TestCase):
    def test_dagster_dev_requires_webserver_dependency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "src/inlumen_dagster_project/components").mkdir(
                parents=True
            )
            (project_root / "src/inlumen_dagster_project/definitions.py").write_text(
                "",
                encoding="utf-8",
            )
            (
                project_root / "src/inlumen_dagster_project/components/shell_command.py"
            ).write_text("", encoding="utf-8")
            (project_root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["dagster==1.13.12"]\n',
                encoding="utf-8",
            )
            (project_root / "Dockerfile").write_text(
                'CMD ["dagster", "dev", "-m", "inlumen_dagster_project.definitions"]\n',
                encoding="utf-8",
            )

            report = validate_dagster_project(project_root, materialize=False)

        self.assertFalse(report["ok"])
        self.assertIn("dagster-webserver", report["errors"][0])

    def test_reviewed_models_are_prefetched_before_definition_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "src/inlumen_dagster_project/components").mkdir(
                parents=True
            )
            (project_root / "src/inlumen_dagster_project/definitions.py").write_text(
                "", encoding="utf-8"
            )
            (
                project_root / "src/inlumen_dagster_project/components/shell_command.py"
            ).write_text("", encoding="utf-8")
            (project_root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["dagster==1.13.12"]\n',
                encoding="utf-8",
            )
            python = project_root / ".inlumen_dagster_validation_venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            (project_root / "model-prefetch.py").write_text(
                "print('prefetch')\n", encoding="utf-8"
            )
            (project_root / "model-requirements.json").write_text(
                '{"schema_version":"inlumen.model-requirements@1","models":[]}',
                encoding="utf-8",
            )
            model_root = project_root / "model-store"
            calls = []

            def fake_run(
                command, *, cwd, timeout_seconds, env=None, execution_id=None
            ):
                calls.append({"command": command, "env": dict(env or {})})
                output = (
                    "prefetched"
                    if str(project_root / "model-prefetch.py") in command
                    else '{"asset_keys": []}'
                )
                return {
                    "command": command,
                    "returncode": 0,
                    "output": output,
                    "ok": True,
                }

            with (
                mock.patch.dict(
                    "os.environ", {"INLUMEN_MODEL_ROOT": str(model_root)}, clear=False
                ),
                mock.patch("app.deployment_validation._run", side_effect=fake_run),
            ):
                report = validate_dagster_project(
                    project_root,
                    materialize=False,
                    skip_install=True,
                )

        self.assertTrue(report["ok"], report)
        self.assertEqual("model_prefetch", report["steps"][0]["name"])
        self.assertEqual("0", calls[0]["env"]["HF_HUB_OFFLINE"])
        self.assertEqual("1", calls[1]["env"]["HF_HUB_OFFLINE"])
        self.assertEqual(
            str(model_root.resolve()), calls[1]["env"]["INLUMEN_MODEL_ROOT"]
        )


class DeploymentInputContractValidationTest(unittest.TestCase):
    def _write_bundle(self, root: Path, *, kind: str = "file") -> None:
        input_dir = root / "inputs"
        node_dir = root / "nodes/node-1-audio-ingestion"
        output_dir = root / "outputs/node-1-audio-ingestion"
        input_dir.mkdir(parents=True)
        node_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        sample_bytes = b"RIFF-sample"
        (input_dir / "sample.wav").write_bytes(sample_bytes)
        (input_dir / "input_manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "inlumen.input-manifest@1",
                    "inputs": [
                        {
                            "filename": "sample.wav",
                            "path": "inputs/sample.wav",
                            "kind": kind,
                            "format": "wav",
                            "size_bytes": len(sample_bytes),
                            "sha256": (
                                "sha256:" + hashlib.sha256(sample_bytes).hexdigest()
                            ),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (node_dir / "node-manifest.json").write_text(
            json.dumps(
                {
                    "data_contract": {
                        "inputs": [
                            {
                                "filename": "sample.wav",
                                "kind": "binary",
                                "format": "wav",
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        (root / "run-spec.json").write_text(
            json.dumps(
                {
                    "schema_version": "inlumen.run-spec@1",
                    "runtime": {"package_manager": "uv"},
                    "node_order": ["1"],
                    "nodes": [{"id": "1"}],
                    "connections": [],
                }
            ),
            encoding="utf-8",
        )
        (root / "bundle-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "inlumen.deployment-bundle@1",
                    "run_spec": "run-spec.json",
                    "targets": {"argo": False, "dagster": False},
                    "nodes": [
                        {
                            "flow_id": "1",
                            "path": "nodes/node-1-audio-ingestion",
                            "output_path": "outputs/node-1-audio-ingestion",
                            "parents": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_filesystem_bundle_accepts_node_owned_root_input_inventory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root, kind="binary")
            node_inputs = root / "inputs/node-1-audio-ingestion"
            node_inputs.mkdir()
            (root / "inputs/sample.wav").replace(node_inputs / "sample.wav")
            (root / "inputs/input_manifest.json").unlink()

            report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertTrue(report["ok"], report["errors"])

    def test_run_outputs_hide_dagster_run_scope_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staged = root / "outputs/node-1/run-uuid/output/result.txt"
            staged.parent.mkdir(parents=True)
            staged.write_text("done", encoding="utf-8")

            outputs = _read_run_output_files(root)

        self.assertEqual("outputs/node-1/result.txt", outputs[0]["path"])

    def test_validation_rejects_run_spec_node_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root, kind="binary")
            run_spec_path = root / "run-spec.json"
            run_spec = json.loads(run_spec_path.read_text(encoding="utf-8"))
            run_spec["nodes"] = [{"id": "other"}]
            run_spec_path.write_text(json.dumps(run_spec), encoding="utf-8")
            report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertFalse(report["ok"])
        self.assertTrue(
            any("node ids do not match" in error for error in report["errors"])
        )

    def test_validation_accepts_current_bundle_and_keeps_v1_v2_compatibility(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root, kind="binary")
            v1_report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )
            run_spec_path = root / "run-spec.json"
            run_spec = json.loads(run_spec_path.read_text(encoding="utf-8"))
            run_spec.update({
                "schema_version": "inlumen.run-spec@2",
                "artifact_contract": {
                    "schema_version": "inlumen.artifact-contract@2",
                },
                "run_inputs": {"path": "inputs", "transport": "filesystem"},
                "outputs": {"path": "outputs", "transport": "filesystem"},
                "engines": {"dagster": None, "argo": None},
            })
            run_spec_path.write_text(json.dumps(run_spec), encoding="utf-8")
            v2_report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

            artifact_contract = {
                "schema_version": "inlumen.artifact-contract@3",
                "transport": "filesystem",
                "input_environment": "PIPELINE_INPUT_DIR",
                "output_environment": "PIPELINE_OUTPUT_DIR",
                "recursive": True,
                "input_layout": "<artifact-relative-path>",
                "output_layout": "<artifact-relative-path>",
                "port_namespaced": False,
                "run_isolation": "<run_id>",
                "source_agnostic": True,
                "connector_agnostic": True,
            }
            run_spec.update({
                "schema_version": "inlumen.run-spec@3",
                "artifact_contract": artifact_contract,
            })
            run_spec_path.write_text(json.dumps(run_spec), encoding="utf-8")
            manifest_path = root / "bundle-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "schema_version": "inlumen.deployment-bundle@2",
                "artifact_contract": artifact_contract,
            })
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            v3_report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertTrue(v1_report["ok"], v1_report["errors"])
        self.assertTrue(v2_report["ok"], v2_report["errors"])
        self.assertTrue(v3_report["ok"], v3_report["errors"])

    def test_current_bundle_rejects_legacy_run_spec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root, kind="binary")
            manifest_path = root / "bundle-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "schema_version": "inlumen.deployment-bundle@2",
                "artifact_contract": {
                    "schema_version": "inlumen.artifact-contract@3",
                    "port_namespaced": False,
                },
            })
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertFalse(report["ok"])
        self.assertIn(
            "inlumen.deployment-bundle@2 requires inlumen.run-spec@3.",
            report["errors"],
        )

    def test_validation_rejects_unknown_bundle_manifest_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root, kind="binary")
            manifest_path = root / "bundle-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = "inlumen.deployment-bundle@99"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertFalse(report["ok"])
        self.assertTrue(any(
            "unsupported schema_version inlumen.deployment-bundle@99" in error
            for error in report["errors"]
        ))

    def test_validation_rejects_unknown_run_spec_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root)
            run_spec_path = root / "run-spec.json"
            run_spec = json.loads(run_spec_path.read_text(encoding="utf-8"))
            run_spec["schema_version"] = "inlumen.run-spec@99"
            run_spec_path.write_text(json.dumps(run_spec), encoding="utf-8")
            report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertFalse(report["ok"])
        self.assertTrue(any(
            "unsupported schema_version inlumen.run-spec@99" in error
            for error in report["errors"]
        ))

    def test_validation_rejects_exported_kind_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root)
            report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertFalse(report["ok"])
        self.assertTrue(
            any("non-canonical kind 'file'" in error for error in report["errors"])
        )
        self.assertTrue(
            any(
                "does not match root node contract" in error
                for error in report["errors"]
            )
        )

    def test_repair_uses_root_node_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root)
            repair = repair_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )
            manifest = json.loads(
                (root / "inputs/input_manifest.json").read_text(encoding="utf-8")
            )
            validation = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertTrue(repair["changed"])
        self.assertEqual("binary", manifest["inputs"][0]["kind"])
        self.assertEqual("wav", manifest["inputs"][0]["format"])
        self.assertTrue(validation["ok"], validation["errors"])

    def test_validation_rejects_input_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root, kind="binary")
            manifest_path = root / "inputs/input_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["inputs"][0]["sha256"] = "sha256:" + ("0" * 64)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertFalse(report["ok"])
        self.assertTrue(any("checksum mismatch" in error for error in report["errors"]))

    def test_validation_rejects_missing_required_root_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_bundle(root, kind="binary")
            (root / "inputs/sample.wav").unlink()
            (root / "inputs/input_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "inlumen.input-manifest@1",
                        "inputs": [],
                    }
                ),
                encoding="utf-8",
            )

            report = validate_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )

        self.assertFalse(report["ok"])
        self.assertTrue(
            any(
                "Root node contract input sample.wav is missing" in error
                for error in report["errors"]
            )
        )

    def test_repair_classifies_multimodal_files_without_descriptors(self):
        expected = {
            "audio.wav": "audio",
            "document.pdf": "document",
            "image.png": "image",
            "notes.txt": "text",
            "records.json": "json",
            "rows.csv": "table",
            "rows.parquet": "table",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "inputs").mkdir(parents=True)
            (root / "nodes").mkdir()
            (root / "outputs").mkdir()
            (root / "bundle-manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "inlumen.deployment-bundle@1",
                        "targets": {"argo": False, "dagster": False},
                        "nodes": [],
                    }
                ),
                encoding="utf-8",
            )
            for filename in expected:
                (root / "inputs" / filename).write_bytes(b"sample")

            repair_deployment_bundle(
                root,
                targets={"argo": False, "dagster": False},
            )
            bundle_manifest = json.loads(
                (root / "bundle-manifest.json").read_text(encoding="utf-8")
            )

        self.assertFalse((root / "inputs/input_manifest.json").exists())
        self.assertEqual(len(expected), bundle_manifest["inputs"]["file_count"])
        self.assertEqual("filesystem", bundle_manifest["inputs"]["transport"])


if __name__ == "__main__":
    unittest.main()
