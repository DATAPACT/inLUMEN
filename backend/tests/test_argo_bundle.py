"""The Argo export is a self-contained bundle that runs outside inLUMEN."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argo_bundle as argo_helpers
from deployment_artifacts import (
    DeploymentArtifactValidationError,
    build_argo_workflow_object,
    build_deployment_bundle_files,
    deployment_bundle_zip,
)
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


def edit_node_manifest(payload, flow_id, change):
    for item in payload["deployment_files"]:
        if item["flow_id"] == flow_id and item["filename"] == "node-manifest.json":
            manifest = json.loads(item["content"])
            change(manifest)
            item["content"] = json.dumps(manifest, indent=2) + "\n"
    return payload


def plugin_payload(base=None):
    def make_plugin_step(manifest):
        manifest["adapter"] = {
            "kind": "task",
            "plugin": {"id": "jsi-wikifier", "version": "1.0.0", "name": "Wikifier"},
            "operation": "annotate",
            "endpoint": "https://wikifier.example.org",
        }

    return edit_node_manifest(base or codegen_payload(), "2", make_plugin_step)


def templates_by_name(workflow):
    return {template["name"]: template for template in workflow["spec"]["templates"]}


def conflicting_payload(models=False):
    """Step 1 needs pandas 1.5, step 2 pandas 2: they cannot share an image."""
    payload = whisper_payload() if models else codegen_payload()
    for item in payload["deployment_files"]:
        if item["filename"] == "requirements.txt":
            item["content"] = "pandas==1.5.3\n" if item["flow_id"] == "1" else "pandas>=2.0\nnumpy\n"
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

    def test_baked_models_skip_the_prefetch_task_and_the_volume(self):
        bundle = argo_bundle(whisper_payload())
        by_path = {item["path"]: item["content"] for item in bundle["files"]}
        dockerfile = by_path["argo/Dockerfile"]

        # The image build runs the same prefetch script, only when asked to.
        self.assertIn("ARG INLUMEN_MODELS=volume", dockerfile)
        self.assertIn('if [ "$INLUMEN_MODELS" = "bake" ]', dockerfile)
        self.assertIn("python /workspace/model-prefetch/model_prefetch.py", dockerfile)
        self.assertIn("INLUMEN_MODEL_ROOT=/models", dockerfile)
        self.assertIn("--mount=type=secret,id=hf_token", dockerfile)
        # Changing step code must not download the models again.
        self.assertLess(dockerfile.index("model_prefetch.py"), dockerfile.index("COPY nodes"))

        baked = by_path["argo/workflow-models-baked.yaml"]
        self.assertNotIn("inlumen-model-prefetch", baked)
        self.assertNotIn("inlumen-model-store", baked)
        self.assertNotIn("volumes:", baked)
        workflow = build_argo_workflow_object(
            CodegenDeploymentArtifactsTest().graph(),
            whisper_payload(),
            shared_runtime=self._shared_runtime(),
            models_baked=True,
        )
        templates = templates_by_name(workflow)
        self.assertNotIn("inlumen-model-prefetch", templates)
        tasks = {task["name"]: task for task in templates["inlumen-pipeline"]["dag"]["tasks"]}
        self.assertEqual(["step-1"], tasks["step-2"]["dependencies"])
        step = templates["step-2"]["container"]
        self.assertNotIn("volumeMounts", step)
        environment = {item["name"]: item.get("value") for item in step["env"]}
        self.assertEqual("/models", environment["INLUMEN_MODEL_ROOT"])
        self.assertIn("asr-profile", [item["name"] for item in workflow["spec"]["arguments"]["parameters"]])

        # The default is still the shared volume.
        self.assertIn("inlumen-model-prefetch", by_path["argo/workflow.yaml"])
        self.assertIn("MODELS ?= volume", by_path["argo/Makefile"])
        self.assertEqual(
            [("argo/workflow.yaml", "cpu", "volume"), ("argo/workflow-models-baked.yaml", "cpu", "image"),
             ("argo/workflow-gpu.yaml", "gpu", "volume"), ("argo/workflow-gpu-models-baked.yaml", "gpu", "image")],
            [(item["path"], item["accelerator"], item["models"]) for item in bundle["manifest"]["argo"]["workflows"]],
        )
        self.assertIn("MODELS=bake", by_path["README.md"])

    def test_pipelines_without_models_or_gpu_steps_get_one_workflow(self):
        bundle = argo_bundle()
        paths = {item["path"] for item in bundle["files"]}
        self.assertEqual({"argo/workflow.yaml"}, {path for path in paths if path.startswith("argo/workflow")})
        self.assertEqual(
            [{"path": "argo/workflow.yaml", "accelerator": "cpu", "models": None}],
            bundle["manifest"]["argo"]["workflows"],
        )
        by_path = {item["path"]: item["content"] for item in bundle["files"]}
        self.assertNotIn("MODELS", by_path["argo/Makefile"])
        self.assertNotIn("ACCELERATOR ?=", by_path["argo/Makefile"])
        self.assertNotIn("INLUMEN_MODELS", by_path["argo/Dockerfile"])
        self.assertNotIn("MODELS=bake", by_path["README.md"])

    def test_steps_get_requests_and_limits_from_their_resource_profile(self):
        workflow = build_argo_workflow_object(
            CodegenDeploymentArtifactsTest().graph(),
            whisper_payload(),
            shared_runtime=self._shared_runtime(),
        )
        templates = templates_by_name(workflow)
        # step-1 needs pandas (standard); step-2 runs a reviewed model (ml_cpu).
        self.assertEqual(
            {"requests": {"cpu": "2", "memory": "2Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
            templates["step-1"]["container"]["resources"],
        )
        self.assertEqual(
            {"requests": {"cpu": "4", "memory": "4Gi"}, "limits": {"cpu": "4", "memory": "4Gi"}},
            templates["step-2"]["container"]["resources"],
        )
        self.assertIn("resources", templates["inlumen-model-prefetch"]["container"])

    def test_resources_a_node_declares_win_over_its_profile(self):
        def declare(manifest):
            manifest["capabilities"] = {"resources": {"cpu": "500m", "memory": "3Gi"}}

        workflow = build_argo_workflow_object(
            CodegenDeploymentArtifactsTest().graph(),
            edit_node_manifest(codegen_payload(), "1", declare),
        )
        self.assertEqual(
            {"requests": {"cpu": "500m", "memory": "3Gi"}, "limits": {"cpu": "500m", "memory": "3Gi"}},
            templates_by_name(workflow)["step-1"]["container"]["resources"],
        )

        def declare_invalid(manifest):
            manifest["capabilities"] = {"resources": {"memory": "lots"}}

        with self.assertRaises(DeploymentArtifactValidationError) as raised:
            build_argo_workflow_object(
                CodegenDeploymentArtifactsTest().graph(),
                edit_node_manifest(codegen_payload(), "1", declare_invalid),
            )
        self.assertIn("capabilities.resources.memory", str(raised.exception.errors))

    def test_gpu_runs_give_model_steps_a_gpu_and_build_cuda_pytorch(self):
        workflow = build_argo_workflow_object(
            CodegenDeploymentArtifactsTest().graph(),
            whisper_payload(),
            shared_runtime=self._shared_runtime(),
            gpu=True,
        )
        templates = templates_by_name(workflow)
        step = templates["step-2"]["container"]
        self.assertEqual("1", step["resources"]["limits"]["nvidia.com/gpu"])
        self.assertNotIn("nvidia.com/gpu", step["resources"]["requests"])
        environment = {item["name"]: item.get("value") for item in step["env"]}
        self.assertEqual("gpu", environment["INLUMEN_ACCELERATOR"])
        prefetch = {item["name"]: item.get("value") for item in templates["inlumen-model-prefetch"]["container"]["env"]}
        self.assertEqual("gpu", prefetch["INLUMEN_ACCELERATOR"])
        # Steps that cannot use a GPU do not hold one.
        self.assertNotIn("nvidia.com/gpu", templates["step-1"]["container"]["resources"]["limits"])

        cpu = build_argo_workflow_object(
            CodegenDeploymentArtifactsTest().graph(), whisper_payload(), shared_runtime=self._shared_runtime(),
        )
        self.assertNotIn("nvidia.com/gpu", json.dumps(cpu))

        by_path = {item["path"]: item["content"] for item in argo_bundle(whisper_payload())["files"]}
        self.assertIn("nvidia.com/gpu", by_path["argo/workflow-gpu.yaml"])
        self.assertNotIn("nvidia.com/gpu", by_path["argo/workflow.yaml"])
        self.assertIn("ACCELERATOR ?= cpu", by_path["argo/Makefile"])
        self.assertIn("--build-arg INLUMEN_ACCELERATOR=$(ACCELERATOR)", by_path["argo/Makefile"])
        self.assertIn("ACCELERATOR=gpu", by_path["README.md"])

    def test_resource_profiles_match_local_runs(self):
        path = Path(__file__).resolve().parents[2] / "codegen" / "app" / "resource_policy.py"
        if not path.is_file():
            self.skipTest("codegen sources are not available")
        spec = importlib.util.spec_from_file_location("codegen_resource_policy", path)
        policy = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = policy  # dataclasses look their module up here
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(policy)

        self.assertEqual(
            {name: {"cpu": item.cpu, "memory_bytes": item.memory_bytes} for name, item in policy.RESOURCE_PROFILES.items()},
            argo_helpers.RESOURCE_PROFILES,
        )
        cases = [
            ([], ""),
            ([], "requests\n"),
            ([], "pandas>=2\n"),
            ([], "torch\n"),
            (["lightweight_cpu"], ""),
            (["heavy_cpu_or_gpu"], ""),
            (["gpu_preferred", "lightweight_cpu"], ""),
            ([""], "pandas\n"),
        ]
        for classes, requirements in cases:
            with self.subTest(classes=classes, requirements=requirements), tempfile.TemporaryDirectory() as root:
                (Path(root) / "requirements.txt").write_text(requirements)
                if classes:
                    (Path(root) / "model-requirements.json").write_text(
                        json.dumps({"models": [{"resource_class": item} for item in classes]})
                    )
                expected, _ = policy.select_resource_profile(Path(root))
                self.assertEqual(
                    expected.name,
                    argo_helpers.select_resource_profile(classes, requirements.splitlines()),
                )

    def test_plugin_base_url_is_a_workflow_parameter(self):
        workflow = build_argo_workflow_object(CodegenDeploymentArtifactsTest().graph(), plugin_payload())
        parameters = {item["name"]: item["value"] for item in workflow["spec"]["arguments"]["parameters"]}
        self.assertEqual("https://wikifier.example.org", parameters["plugin-url-step-2"])
        template = templates_by_name(workflow)["step-2"]
        environment = {item["name"]: item.get("value") for item in template["container"]["env"]}
        self.assertEqual("{{workflow.parameters.plugin-url-step-2}}", environment["INLUMEN_PLUGIN_BASE_URL"])
        self.assertEqual("jsi-wikifier@1.0.0", template["metadata"]["annotations"]["inlumen.ai/plugin"])
        self.assertNotIn("INLUMEN_PLUGIN_BASE_URL", json.dumps(templates_by_name(workflow)["step-1"]))

        bundle = argo_bundle(plugin_payload())
        self.assertEqual(
            [{
                "flow_id": "2",
                "plugin": "jsi-wikifier@1.0.0",
                "parameter": "plugin-url-step-2",
                "environment": "INLUMEN_PLUGIN_URL_STEP_2",
                "default_url": "https://wikifier.example.org",
            }],
            bundle["manifest"]["argo"]["plugin_endpoints"],
        )
        by_path = {item["path"]: item["content"] for item in bundle["files"]}
        self.assertIn("plugin-url-step-2|INLUMEN_PLUGIN_URL_STEP_2", by_path["argo/submit.sh"])
        readme = by_path["README.md"]
        self.assertIn("### Plugin steps", readme)
        self.assertIn("egress", readme)
        self.assertIn("DNS", readme)
        self.assertIn("| `2` | jsi-wikifier@1.0.0 | `plugin-url-step-2` | `INLUMEN_PLUGIN_URL_STEP_2` |", readme)
        self.assertNotIn("### Plugin steps", {item["path"]: item["content"] for item in argo_bundle()["files"]}["README.md"])


    def _shared_runtime(self, payload=None):
        from deployment_artifacts import (
            _shared_argo_runtime,
            extract_pipeline_steps,
            select_runtime_steps,
        )

        steps = select_runtime_steps(extract_pipeline_steps(CodegenDeploymentArtifactsTest().graph()))
        return _shared_argo_runtime(steps, [step["flow_id"] for step in steps], payload or whisper_payload())

    def test_invalid_workflows_are_reported_at_export_without_network(self):
        import deployment_artifacts

        real_builder = deployment_artifacts.build_argo_workflow_object

        def broken(break_workflow):
            def build(*args, **kwargs):
                workflow = real_builder(*args, **kwargs)
                break_workflow(workflow)
                return workflow
            return build

        def missing_template(workflow):
            workflow["spec"]["templates"] = [
                template for template in workflow["spec"]["templates"] if template["name"] != "step-2"
            ]

        def missing_dependency(workflow):
            tasks = workflow["spec"]["templates"][0]["dag"]["tasks"]
            next(task for task in tasks if task["name"] == "step-2")["dependencies"] = ["step-9"]

        def missing_image(workflow):
            del workflow["spec"]["templates"][1]["container"]["image"]

        for break_workflow, message in (
            (missing_template, "references missing template 'step-2'"),
            (missing_dependency, "depends on missing task 'step-9'"),
            (missing_image, "is missing image"),
        ):
            with self.subTest(message=message), \
                    patch.object(deployment_artifacts, "build_argo_workflow_object", broken(break_workflow)), \
                    patch("socket.socket", side_effect=AssertionError("export must not use the network")):
                with self.assertRaises(deployment_artifacts.DeploymentArtifactValidationError) as raised:
                    argo_bundle()
                self.assertIn(message, " ".join(raised.exception.errors))

    def test_requirement_conflicts_are_found_offline(self):
        from deployment_artifacts import _requirement_conflict

        for first, second, conflict in (
            (["pandas==1.5.3"], ["pandas>=2.0"], "pandas"),
            (["pandas==1.5.3"], ["pandas==2.2.0"], "pandas"),
            (["numpy<1.25"], ["numpy>=1.26"], "numpy"),
            (["Torch==2.1.0"], ["torch==2.2.0"], "torch"),
            (["pandas>=1.5,<3"], ["pandas>=2.0"], ""),
            (["pandas>1.0"], ["pandas<1.1"], ""),
            (["pandas==1.5.*"], ["pandas>=1.5.2,<1.6"], ""),
            (["pandas"], ["pandas==2.2.0"], ""),
            (["pandas==1.5.3"], ["numpy==1.26.0"], ""),
            (["-r extra.txt", "--index-url https://x"], ["pandas==2.0"], ""),
        ):
            with self.subTest(first=first, second=second):
                self.assertEqual(conflict, _requirement_conflict(first, second))

    def test_steps_with_conflicting_packages_get_separate_images(self):
        bundle = argo_bundle(conflicting_payload())
        by_path = {item["path"]: item["content"] for item in bundle["files"]}
        manifest = bundle["manifest"]["argo"]

        self.assertEqual("grouped", manifest["image_strategy"])
        self.assertNotIn("argo/Dockerfile", by_path)
        self.assertEqual(
            [("env-1", ["1"], []), ("env-2", ["2"], [{"flow_ids": ["1"], "package": "pandas"}])],
            [(image["group"], image["flow_ids"], image["conflicts"]) for image in manifest["images"]],
        )
        self.assertIn("pandas==1.5.3", by_path["argo/images/env-1/requirements.txt"])
        self.assertNotIn("pandas>=2.0", by_path["argo/images/env-1/requirements.txt"])
        self.assertIn("pandas>=2.0", by_path["argo/images/env-2/requirements.txt"])
        self.assertIn(
            "COPY argo/images/env-2/requirements.txt argo/constraints.txt argo/images/env-2/requirements.lock*",
            by_path["argo/images/env-2/Dockerfile"],
        )

        workflow = by_path["argo/workflow.yaml"]
        self.assertIn('image: "{{workflow.parameters.pipeline-image-env-1}}"', workflow)
        self.assertIn('image: "{{workflow.parameters.pipeline-image-env-2}}"', workflow)
        self.assertNotIn('name: "pipeline-image"', workflow)
        templates = {
            template["name"]: template
            for template in build_argo_workflow_object(
                CodegenDeploymentArtifactsTest().graph(),
                conflicting_payload(),
                shared_runtime=self._shared_runtime(conflicting_payload()),
            )["spec"]["templates"]
        }
        self.assertEqual("{{workflow.parameters.pipeline-image-env-1}}", templates["step-1"]["container"]["image"])
        self.assertEqual("{{workflow.parameters.pipeline-image-env-2}}", templates["step-2"]["container"]["image"])

        readme = by_path["README.md"]
        self.assertIn("runs on **2 images**", readme)
        self.assertIn("| `env-2` | Preprocessing | `pandas` |", readme)
        self.assertIn("make -C argo image push", by_path["ci/github-actions/build-image.yml"])

    def test_model_prefetch_runs_on_the_image_of_a_model_step(self):
        bundle = argo_bundle(conflicting_payload(models=True))
        by_path = {item["path"]: item["content"] for item in bundle["files"]}
        self.assertIn("huggingface-hub", by_path["argo/images/env-2/requirements.txt"])
        self.assertNotIn("huggingface-hub", by_path["argo/images/env-1/requirements.txt"])
        workflow = build_argo_workflow_object(
            CodegenDeploymentArtifactsTest().graph(),
            conflicting_payload(models=True),
            shared_runtime=self._shared_runtime(conflicting_payload(models=True)),
        )
        prefetch = next(t for t in workflow["spec"]["templates"] if t["name"] == "inlumen-model-prefetch")
        self.assertEqual("{{workflow.parameters.pipeline-image-env-2}}", prefetch["container"]["image"])

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

    def test_make_builds_and_submits_every_image_group(self):
        root = (Path(self.temp.name) / "grouped").resolve()
        with zipfile.ZipFile(io.BytesIO(deployment_bundle_zip(argo_bundle(conflicting_payload())["files"]))) as archive:
            archive.extractall(root)
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        fake_tool(self.bin, "docker", 'echo "docker $@" >> "$CALLS"\n')
        fake_tool(self.bin, "argo", 'echo "argo $@" >> "$CALLS"\n')
        tags = {
            image["group"]: image["image"].split(":")[1]
            for image in argo_bundle(conflicting_payload())["manifest"]["argo"]["images"]
        }

        def make(*arguments):
            return subprocess.run(
                ["make", "--no-print-directory", "-s", "-C", "argo", *arguments, "REGISTRY=ghcr.io/acme", "ARGO_FLAGS="],
                cwd=root, env=self.env, capture_output=True, text=True, check=False,
            )

        printed = make("print-image")
        self.assertEqual(
            [f"ghcr.io/acme/inlumen-pipeline-env-1:{tags['env-1']}", f"ghcr.io/acme/inlumen-pipeline-env-2:{tags['env-2']}"],
            printed.stdout.split(),
            printed.stderr,
        )
        for target in ("image", "push", "submit"):
            result = make(target)
            self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls.read_text()
        self.assertIn(f"docker build -f {root}/argo/images/env-2/Dockerfile -t ghcr.io/acme/inlumen-pipeline-env-2:{tags['env-2']} {root}", calls)
        self.assertIn(f"docker push ghcr.io/acme/inlumen-pipeline-env-1:{tags['env-1']}", calls)
        submit = next(line for line in calls.splitlines() if line.startswith("argo submit"))
        self.assertIn(f"-p pipeline-image-env-1=ghcr.io/acme/inlumen-pipeline-env-1:{tags['env-1']}", submit)
        self.assertIn(f"-p pipeline-image-env-2=ghcr.io/acme/inlumen-pipeline-env-2:{tags['env-2']}", submit)
        self.assertNotIn("pipeline-image=", submit)

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


@unittest.skipUnless(HAS_SHELL_TOOLS, "bash, make and tar are required")
class ArgoBundleOptionsScriptsTest(ArgoBundleScriptsTest):
    """make -C argo with MODELS, ACCELERATOR and plugin URLs, on a model pipeline."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.root)
        self.bundle = argo_bundle(plugin_payload(whisper_payload()))
        with zipfile.ZipFile(io.BytesIO(deployment_bundle_zip(self.bundle["files"]))) as archive:
            archive.extractall(self.root)
        self.tag = self.bundle["manifest"]["argo"]["image_tag"]

    def make(self, *arguments, env=None):
        return self.run_in_bundle("make", "--no-print-directory", "-s", "-C", "argo", *arguments, env=env)

    def test_options_pick_the_image_tag_and_workflow(self):
        for arguments, tag, workflow in (
            ((), self.tag, "workflow.yaml"),
            (("MODELS=bake",), f"{self.tag}-models", "workflow-models-baked.yaml"),
            (("MODELS=bake", "ASR_PROFILE=fast"), f"{self.tag}-models-fast", "workflow-models-baked.yaml"),
            (("ACCELERATOR=gpu",), f"{self.tag}-gpu", "workflow-gpu.yaml"),
            (("ACCELERATOR=gpu", "MODELS=bake"), f"{self.tag}-gpu-models", "workflow-gpu-models-baked.yaml"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(f"inlumen-pipeline:{tag}", self.make("print-image", *arguments).stdout.strip())
                printed = self.make("print-workflow", *arguments).stdout.strip()
                self.assertEqual(workflow, Path(printed).name)
                self.assertTrue(Path(printed).is_file(), printed)

        wrong = self.make("print-image", "MODELS=pvc")
        self.assertNotEqual(0, wrong.returncode)
        self.assertIn("MODELS must be volume or bake", wrong.stderr)

    def test_image_build_passes_the_options_and_the_token_as_a_secret(self):
        fake_tool(self.bin, "docker", 'echo "$@" >> "$CALLS"\n')
        result = self.make(
            "image", "MODELS=bake", "ACCELERATOR=gpu", "ASR_PROFILE=fast",
            env={**self.env, "HF_TOKEN": "hf_do_not_print"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        call = self.calls.read_text()
        for argument in (
            "--build-arg INLUMEN_ACCELERATOR=gpu",
            "--build-arg INLUMEN_MODELS=bake",
            "--build-arg INLUMEN_ASR_PROFILE=fast",
            "--secret id=hf_token,env=HF_TOKEN",
            f"-t inlumen-pipeline:{self.tag}-gpu-models-fast",
        ):
            self.assertIn(argument, call)
        self.assertNotIn("hf_do_not_print", call)

        self.calls.unlink()
        self.env.pop("HF_TOKEN", None)
        self.assertEqual(0, self.make("image").returncode)
        call = self.calls.read_text()
        self.assertIn("--build-arg INLUMEN_MODELS=volume", call)
        self.assertIn("--build-arg INLUMEN_ACCELERATOR=cpu", call)
        self.assertNotIn("--secret", call)

    def test_setup_creates_the_model_volume_only_without_baked_models(self):
        fake_tool(self.bin, "kubectl", 'echo "$@" >> "$CALLS"\n')
        self.assertEqual(0, self.make("setup").returncode)
        self.assertIn("model-store-pvc.yaml", self.calls.read_text())
        self.calls.unlink()
        result = self.make("setup", "MODELS=bake")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("model-store-pvc.yaml", self.calls.read_text())

    def test_submit_uses_the_chosen_workflow_profile_and_plugin_url(self):
        fake_tool(self.bin, "argo", 'echo "$@" >> "$CALLS"\n')
        (self.root / ".env").write_text("INLUMEN_PLUGIN_URL_STEP_2=http://wikifier.tools.svc:8080\n")
        result = self.make("submit", "MODELS=bake", "ASR_PROFILE=fast", "ARGO_FLAGS=")
        self.assertEqual(0, result.returncode, result.stderr)
        call = self.calls.read_text().strip()
        self.assertIn("argo/workflow-models-baked.yaml -n argo", call)
        self.assertIn(f"-p pipeline-image=inlumen-pipeline:{self.tag}-models-fast", call)
        self.assertIn("-p asr-profile=fast", call)
        self.assertIn("-p plugin-url-step-2=http://wikifier.tools.svc:8080", call)

    # The base class's checks of the default bundle do not apply here.
    test_make_prints_the_image_name = None
    test_create_secrets_reads_env_file_and_requires_missing_values = None
    test_submit_passes_image_output_prefix_and_service_account = None

if __name__ == "__main__":
    unittest.main()
