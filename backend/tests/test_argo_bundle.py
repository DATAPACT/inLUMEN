"""The Argo export is a self-contained bundle that runs outside inLUMEN."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deployment_artifacts import build_argo_workflow_object, build_deployment_bundle_files, deployment_bundle_zip
from model_plans import FASTER_WHISPER_PLAN
from test_codegen_deployment_artifacts import (
    CodegenDeploymentArtifactsTest,
    codegen_payload,
    node_manifest_content,
)

HAS_SHELL_TOOLS = all(shutil.which(tool) for tool in ("bash", "make", "tar"))


def argo_bundle(payload=None):
    return build_deployment_bundle_files(
        CodegenDeploymentArtifactsTest().graph(),
        payload or codegen_payload(),
        targets={"argo": True, "dagster": False},
    )


def whisper_payload():
    payload = codegen_payload()
    for item in payload["deployment_files"]:
        if item["flow_id"] == "2" and item["filename"] == "node-manifest.json":
            item["content"] = node_manifest_content(deepcopy(FASTER_WHISPER_PLAN))
    return payload


def fake_tool(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


class ArgoBundleLayoutTest(unittest.TestCase):
    def test_bundle_has_everything_needed_to_build_and_run(self):
        bundle = argo_bundle()
        by_path = {item["path"]: item for item in bundle["files"]}

        for path in (
            "argo/workflow.yaml",
            "argo/Dockerfile",
            "argo/requirements.txt",
            "argo/constraints.txt",
            "argo/Makefile",
            "argo/submit.sh",
            "argo/create-secrets.sh",
            "argo/k8s/rbac.yaml",
            "argo/k8s/artifact-repositories.yaml",
            "argo/k8s/runtime-secrets.example.yaml",
            "ci/github-actions/build-image.yml",
            ".gitignore",
            ".dockerignore",
            ".env.example",
            "README.md",
        ):
            self.assertIn(path, by_path)
        self.assertNotIn("argo/upload-inputs.sh", by_path)
        for script in ("argo/submit.sh", "argo/create-secrets.sh"):
            self.assertTrue(by_path[script].get("executable"), script)
        self.assertNotIn("dagster/Dockerfile", by_path)

        manifest = bundle["manifest"]["argo"]
        self.assertEqual("shared", manifest["image_strategy"])
        self.assertEqual(manifest["environment_hash"][:12], manifest["image_tag"])
        self.assertIn(f"EXPORT_TAG := {manifest['image_tag']}", by_path["argo/Makefile"]["content"])
        self.assertEqual(
            {"transport": "image", "path": "inputs", "image_path": "/workspace/inputs"},
            manifest["inputs"],
        )
        self.assertEqual(
            [{"flow_id": "1", "key": "1.api_key", "environment": "INLUMEN_SECRET_1_API_KEY", "required": True}],
            manifest["secret_keys"],
        )

        workflow = by_path["argo/workflow.yaml"]["content"]
        self.assertIn('name: "inlumen-artifact-repositories"', workflow.replace("configMap", "name"))
        self.assertIn('"1.api_key"', workflow)
        self.assertIn("name: inlumen-artifact-repositories", by_path["argo/k8s/artifact-repositories.yaml"]["content"])
        self.assertIn("INLUMEN_SECRET_1_API_KEY=", by_path[".env.example"]["content"])
        self.assertIn('"1.api_key": ""  # .env: INLUMEN_SECRET_1_API_KEY (required)',
                      by_path["argo/k8s/runtime-secrets.example.yaml"]["content"])

        readme = by_path["README.md"]["content"]
        self.assertIn("inLUMEN's job ends with this bundle", readme)
        self.assertIn("make -C argo image k3d-import", readme)
        self.assertIn("make -C argo secrets", readme)
        self.assertIn(".env", by_path[".gitignore"]["content"])

    def test_image_installs_cpu_only_pytorch_by_default(self):
        by_path = {item["path"]: item["content"] for item in argo_bundle()["files"]}
        dockerfile = by_path["argo/Dockerfile"]
        self.assertIn("ARG INLUMEN_ACCELERATOR=cpu", dockerfile)
        self.assertIn("https://download.pytorch.org/whl/cpu", dockerfile)
        self.assertLess(dockerfile.index("INLUMEN_PYTORCH_CPU_INDEX_URL\" \"$TORCH_REQUIREMENT"),
                        dockerfile.index("requirements.lock;"))
        self.assertIn("--torch-backend $(TORCH_BACKEND)", by_path["argo/Makefile"])
        self.assertIn("TORCH_BACKEND ?= cpu", by_path["argo/Makefile"])

    def test_root_steps_read_inputs_copied_into_the_image(self):
        by_path = {item["path"]: item["content"] for item in argo_bundle()["files"]}
        self.assertIn("COPY inputs /workspace/inputs", by_path["argo/Dockerfile"])

        workflow = by_path["argo/workflow.yaml"]
        self.assertNotIn("run-inputs", workflow)
        self.assertNotIn("input-artifact-key", workflow)
        self.assertNotIn("inlumen/input/", workflow)
        self.assertIn('value: "/workspace/inputs/node-1-ingestion"', workflow)
        # Downstream steps still receive upstream artifacts through Argo.
        self.assertIn('path: "/inlumen/staging/input"', workflow)

        readme = by_path["README.md"]
        self.assertIn("Keep the image private", readme)
        self.assertNotIn("make -C argo inputs", readme)
        self.assertNotIn("inputs:", by_path["argo/Makefile"])

    def test_root_steps_without_files_still_get_an_input_directory(self):
        payload = codegen_payload()
        payload["deployment_files"] = [
            item for item in payload["deployment_files"]
            if not (item["flow_id"] == "1" and item["filename"] in ("vital_signs_short.csv", "sample.wav", "extra.txt"))
        ]
        for item in payload["deployment_files"]:
            if item["flow_id"] == "1" and item["filename"] == "node-manifest.json":
                manifest = json.loads(item["content"])
                manifest["data_contract"]["inputs"] = []
                item["content"] = json.dumps(manifest)
            if item["flow_id"] == "1" and item["filename"] == "requirements.txt":
                item["content"] = "pandas\n"

        paths = {item["path"] for item in argo_bundle(payload)["files"]}

        self.assertIn("inputs/node-1-ingestion/.gitkeep", paths)

    def test_reviewed_models_are_prefetched_into_a_shared_read_only_store(self):
        bundle = argo_bundle(whisper_payload())
        by_path = {item["path"]: item["content"] for item in bundle["files"]}

        for path in (
            "argo/model-prefetch/model-requirements.json",
            "argo/model-prefetch/model_prefetch.py",
            "argo/k8s/model-store-pvc.yaml",
        ):
            self.assertIn(path, by_path)
        requirements = json.loads(by_path["argo/model-prefetch/model-requirements.json"])
        self.assertEqual("faster-whisper", requirements["models"][0]["adapter_id"])
        self.assertIn("COPY argo/model-prefetch /workspace/model-prefetch", by_path["argo/Dockerfile"])
        self.assertIn("huggingface-hub", by_path["argo/requirements.txt"])
        self.assertIn("claimName: \"inlumen-model-store\"", by_path["argo/workflow.yaml"])
        self.assertIn("kubectl apply -n $(NAMESPACE) -f $(ARGO_DIR)/k8s/model-store-pvc.yaml",
                      by_path["argo/Makefile"])
        self.assertIn("HF_TOKEN=", by_path[".env.example"])
        self.assertIn("inlumen-model-prefetch", by_path["README.md"])

        workflow = build_argo_workflow_object(
            CodegenDeploymentArtifactsTest().graph(),
            whisper_payload(),
            shared_runtime=self._shared_runtime(),
        )
        tasks = {task["name"]: task for task in workflow["spec"]["templates"][0]["dag"]["tasks"]}
        templates = {template["name"]: template for template in workflow["spec"]["templates"]}
        self.assertEqual(["inlumen-model-prefetch", "step-1"], tasks["step-2"]["dependencies"])
        self.assertNotIn("inlumen-model-prefetch", tasks["step-1"].get("dependencies", []))

        prefetch = templates["inlumen-model-prefetch"]["container"]
        self.assertEqual(["python", "/workspace/model-prefetch/model_prefetch.py"], prefetch["command"])
        self.assertEqual([{"name": "model-store", "mountPath": "/models"}], prefetch["volumeMounts"])
        token = next(item for item in prefetch["env"] if item["name"] == "HF_TOKEN")
        self.assertTrue(token["valueFrom"]["secretKeyRef"]["optional"])

        step = templates["step-2"]["container"]
        self.assertEqual(
            [{"name": "model-store", "mountPath": "/models", "readOnly": True}],
            step["volumeMounts"],
        )
        environment = {item["name"]: item.get("value") for item in step["env"]}
        self.assertEqual("/models", environment["INLUMEN_MODEL_ROOT"])
        self.assertEqual("{{workflow.parameters.asr-profile}}", environment["INLUMEN_ASR_PROFILE"])
        self.assertNotIn("volumeMounts", templates["step-1"]["container"])
        self.assertEqual(
            [{"name": "model-store", "persistentVolumeClaim": {"claimName": "inlumen-model-store"}}],
            workflow["spec"]["volumes"],
        )

    def test_pipelines_without_models_have_no_model_store(self):
        bundle = argo_bundle()
        paths = {item["path"] for item in bundle["files"]}
        self.assertNotIn("argo/k8s/model-store-pvc.yaml", paths)
        self.assertIsNone(bundle["manifest"]["argo"]["model_store"])
        workflow = {item["path"]: item["content"] for item in bundle["files"]}["argo/workflow.yaml"]
        self.assertNotIn("inlumen-model-prefetch", workflow)
        self.assertNotIn("volumes:", workflow)

    def _shared_runtime(self):
        from deployment_artifacts import (
            _shared_argo_runtime,
            extract_pipeline_steps,
            select_runtime_steps,
        )

        steps = select_runtime_steps(extract_pipeline_steps(CodegenDeploymentArtifactsTest().graph()))
        return _shared_argo_runtime(steps, [step["flow_id"] for step in steps], whisper_payload())

    def test_bundle_never_contains_secret_values(self):
        bundle = argo_bundle()
        self.assertNotIn("do-not-export", json.dumps(bundle))

    def test_image_tag_is_stable_and_follows_code_and_requirements(self):
        tag = argo_bundle()["manifest"]["argo"]["image_tag"]
        self.assertEqual(tag, argo_bundle()["manifest"]["argo"]["image_tag"])

        changed_code = codegen_payload()
        next(item for item in changed_code["deployment_files"] if item["path"] == "nodes/2/main.py")["content"] = "print('v2')\n"
        self.assertNotEqual(tag, argo_bundle(changed_code)["manifest"]["argo"]["image_tag"])

        changed_input = codegen_payload()
        next(item for item in changed_input["deployment_files"] if item["filename"] == "vital_signs_short.csv")["content"] = "heart_rate\n80\n"
        self.assertNotEqual(tag, argo_bundle(changed_input)["manifest"]["argo"]["image_tag"])

        changed_requirements = codegen_payload()
        next(item for item in changed_requirements["deployment_files"] if item["path"] == "nodes/2/requirements.txt")["content"] = "pandas\nnumpy\nrequests\n"
        self.assertNotEqual(tag, argo_bundle(changed_requirements)["manifest"]["argo"]["image_tag"])

    def test_what_reaches_a_destination_is_kept_under_the_run_output_prefix(self):
        graph = CodegenDeploymentArtifactsTest().graph()
        graph["nodes"][1]["data"]["type"] = "destination"
        graph["nodes"][1]["data"]["ports"] = {
            "inputs": [{"id": "data", "name": "data", "type": "Any", "required": True}],
            "outputs": [],
        }

        workflow = build_argo_workflow_object(graph, codegen_payload())

        templates = {template["name"]: template for template in workflow["spec"]["templates"]}
        self.assertEqual(
            [{
                "name": "data",
                "path": "/inlumen/outputs/data",
                "archive": {"none": {}},
                "s3": {"key": "{{workflow.parameters.output-artifact-prefix}}/step-2/data"},
            }],
            templates["step-1"]["outputs"]["artifacts"],
        )
        self.assertNotIn("outputs", templates["step-2"])

    def test_zip_keeps_paths_binary_content_and_script_modes(self):
        bundle = argo_bundle()
        archive = zipfile.ZipFile(io.BytesIO(deployment_bundle_zip(bundle["files"])))
        self.assertEqual(0o755, archive.getinfo("argo/submit.sh").external_attr >> 16)
        self.assertEqual(0o644, archive.getinfo("argo/workflow.yaml").external_attr >> 16)
        self.assertEqual(b"RIFF\xff\x00\x80WAVEfmt \x00data", archive.read("inputs/node-1-ingestion/sample.wav"))
        self.assertEqual(deployment_bundle_zip(bundle["files"]), deployment_bundle_zip(deepcopy(bundle["files"])))
        with self.assertRaises(ValueError):
            deployment_bundle_zip([{"path": "../escape.txt", "content": ""}])


@unittest.skipUnless(HAS_SHELL_TOOLS, "bash, make and tar are required")
class ArgoBundleScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "bundle"
        with zipfile.ZipFile(io.BytesIO(deployment_bundle_zip(argo_bundle()["files"]))) as archive:
            archive.extractall(self.root)
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self.calls = Path(self.temp.name) / "calls"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "CALLS": str(self.calls),
        }
        self.env.pop("INLUMEN_SECRET_1_API_KEY", None)

    def tearDown(self):
        self.temp.cleanup()

    def run_in_bundle(self, *command, env=None):
        return subprocess.run(
            list(command),
            cwd=self.root,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_scripts_are_valid_bash(self):
        for script in sorted((self.root / "argo").glob("*.sh")):
            result = self.run_in_bundle("bash", "-n", str(script))
            self.assertEqual(0, result.returncode, f"{script.name}: {result.stderr}")

    def test_make_prints_the_image_name(self):
        tag = argo_bundle()["manifest"]["argo"]["image_tag"]
        result = self.run_in_bundle("make", "--no-print-directory", "-s", "-C", "argo", "print-image")
        self.assertEqual(f"inlumen-pipeline:{tag}", result.stdout.strip(), result.stderr)
        result = self.run_in_bundle(
            "make", "--no-print-directory", "-s", "-C", "argo", "print-image", "REGISTRY=ghcr.io/acme",
        )
        self.assertEqual(f"ghcr.io/acme/inlumen-pipeline:{tag}", result.stdout.strip())

        (self.root / "argo" / "requirements.lock").write_text("pandas==2.2.3\n")
        result = self.run_in_bundle("make", "--no-print-directory", "-s", "-C", "argo", "print-image")
        self.assertRegex(result.stdout.strip(), rf"^inlumen-pipeline:{tag}-\d+$")

    def test_create_secrets_reads_env_file_and_requires_missing_values(self):
        fake_tool(
            self.bin,
            "kubectl",
            'echo "$@" >> "$CALLS"\n'
            'for arg in "$@"; do case "$arg" in --from-file=*) f="${arg#*=}"; f="${f#*=}"; '
            'echo "value=$(cat "$f")" >> "$CALLS";; esac; done\n',
        )

        missing = self.run_in_bundle("bash", "argo/create-secrets.sh")
        self.assertNotEqual(0, missing.returncode)
        self.assertIn("INLUMEN_SECRET_1_API_KEY", missing.stderr)

        (self.root / ".env").write_text('# local values\nINLUMEN_SECRET_1_API_KEY="s3cret value"\n')
        result = self.run_in_bundle("bash", "argo/create-secrets.sh")

        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls.read_text()
        self.assertIn("create secret generic inlumen-runtime-secrets -n argo --from-file=1.api_key=", calls)
        self.assertIn("value=s3cret value", calls)
        # Values travel through files, never as command-line arguments.
        self.assertNotIn("s3cret value --", calls)
        self.assertNotIn("from-literal", calls)

    def test_submit_passes_image_output_prefix_and_service_account(self):
        fake_tool(self.bin, "argo", 'echo "$@" >> "$CALLS"\n')

        result = self.run_in_bundle(
            "make", "--no-print-directory", "-s", "-C", "argo", "submit",
            "REGISTRY=ghcr.io/acme", "ARGO_FLAGS=", "NAMESPACE=pipelines",
            env={**self.env, "RUN_ID": "run-1"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        tag = argo_bundle()["manifest"]["argo"]["image_tag"]
        call = self.calls.read_text().strip()
        self.assertTrue(call.startswith("submit "), call)
        self.assertIn("argo/workflow.yaml -n pipelines --serviceaccount inlumen-workflow", call)
        self.assertIn(f"-p pipeline-image=ghcr.io/acme/inlumen-pipeline:{tag}", call)
        self.assertIn("-p output-artifact-prefix=inlumen/output/run-1", call)


if __name__ == "__main__":
    unittest.main()
