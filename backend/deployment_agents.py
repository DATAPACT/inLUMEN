import base64
import inspect
import json
import mimetypes
import re
from pathlib import PurePosixPath
from typing import Any, Optional

from autogen_core.models import SystemMessage, UserMessage
from pydantic import BaseModel, Field

from attachment_validation import attachment_input_errors
from async_runtime import run_async
from deployment_artifacts import (
    DeploymentArtifactValidationError,
    _argo_name,
    _extract_json_cmd_from_dockerfile,
    _sanitize_fragment,
    build_argo_workflow_yaml,
    extract_pipeline_edges,
    extract_pipeline_steps,
    select_runtime_steps,
    validate_dockerfile_artifacts,
)
from generators.registry import GeneratorRegistry
from llm_config import LLMConfig, resolve_llm_config, select_model_client
from minio_gateway import read_minio_object, read_minio_object_bytes

CODEGEN_GENERATOR = "inlumen-codegen-service"
RUNTIME_ATTACHMENT_NAMES = {
    "requirements.txt",
    "node-manifest.json",
    "validation-report.json",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "poetry.lock",
}
RUNTIME_ATTACHMENT_SUFFIXES = {
    ".py",
    ".pyi",
    ".sh",
    ".bash",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
}
PYTHON_ENTRYPOINT_NAMES = (
    "main.py",
    "app.py",
    "run.py",
    "process.py",
    "pipeline.py",
    "script.py",
)


class ListDockerfilesResponse(BaseModel):
    class DockerfileItem(BaseModel):
        dockerfile_filename: str
        content: str
        flow_id: Optional[str] = None
        image: Optional[str] = None
        command: list[str] = Field(default_factory=list)
        files: list[str] = Field(default_factory=list)
        generator: Optional[str] = None
        configuration_hash: Optional[str] = None
        build_manifest: Optional[str] = None

    class GuardrailReport(BaseModel):
        valid: bool
        checks: list[str] = Field(default_factory=list)

    dockerfiles: list[DockerfileItem]
    runtime_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    deployment_files: list[dict[str, Any]] = Field(default_factory=list)
    guardrails: Optional[GuardrailReport] = None


async def generate_dockerfiles_with_agent(
    filenames: list[str],
    ids: list[str],
    llm_config: Optional[LLMConfig] = None,
    pipeline_graph: Optional[dict] = None,
    file_refs: Optional[list[dict]] = None,
    *,
    require_attached_runtime: bool = False,
) -> ListDockerfilesResponse:
    """Generate deterministic Dockerfiles where registered, using the LLM otherwise."""
    if file_refs is None:
        if len(filenames) != len(ids):
            raise ValueError("filenames and ids must have the same length.")
        file_refs = [
            {
                "filename": filename,
                "bucket": f"files-step-id-{step_id}",
                "step_id": step_id,
            }
            for filename, step_id in zip(filenames, ids)
        ]

    graph_steps = extract_pipeline_steps(pipeline_graph)
    if graph_steps:
        # The files attached to the current graph are authoritative. A global file
        # listing can contain files from another pipeline/version with the same
        # step id and must not leak into this deployment.
        all_steps = graph_steps
        file_refs = [
            dict(file_ref)
            for step in graph_steps
            for file_ref in step.get("files") or []
            if isinstance(file_ref, dict)
        ]
    else:
        all_steps = extract_pipeline_steps(pipeline_graph, file_refs)
    steps = select_runtime_steps(all_steps)
    if not steps:
        raise ValueError("No pipeline steps were found for Dockerfile generation.")

    generator_registry = GeneratorRegistry()
    deterministic_bundles = []
    codegen_runtime_artifacts: list[dict[str, Any]] = []
    codegen_dockerfiles: list[dict[str, Any]] = []
    generic_steps = []
    artifact_errors: list[str] = []
    for step in steps:
        # For Dagster, the files attached to the node are the runtime source.
        # They may have been uploaded manually or produced by any external tool.
        # A generated_artifact record is useful metadata, but is not required.
        codegen_artifact = (
            _codegen_artifact_from_persisted_files(step)
            if require_attached_runtime
            else _codegen_artifact_for_step(step)
        )
        if codegen_artifact is None and not require_attached_runtime:
            codegen_artifact = _codegen_artifact_from_persisted_files(step)
        if codegen_artifact is not None:
            try:
                runtime_artifact, dockerfile = await _read_persisted_codegen_artifact(
                    step,
                    codegen_artifact,
                )
            except DeploymentArtifactValidationError as exc:
                artifact_errors.extend(exc.errors)
                continue
            codegen_runtime_artifacts.append(runtime_artifact)
            codegen_dockerfiles.append(dockerfile)
            continue

        if require_attached_runtime:
            artifact_errors.append(
                f"Node {step.get('flow_id') or '<unknown>'} cannot be exported to Dagster "
                "until it has an attached Python script (*.py). requirements.txt is "
                "optional; InLumen generates the remaining Dagster packaging."
            )
            continue

        generator = generator_registry.generator_for_step(step)
        if generator is None:
            generic_steps.append(step)
            continue
        deterministic_bundles.append(generator.generate(step, pipeline_graph))

    if artifact_errors:
        raise DeploymentArtifactValidationError(
            "Persisted codegen runtime artifact validation failed",
            artifact_errors,
        )

    deterministic_dockerfiles = [
        bundle.dockerfile_artifact()
        for bundle in deterministic_bundles
    ]
    generic_dockerfiles: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    if generic_steps:
        resolved_config = llm_config or resolve_llm_config()
        generic_step_ids = {str(step["flow_id"]) for step in generic_steps}
        generic_file_refs = [
            file_ref
            for file_ref in file_refs
            if str(file_ref.get("step_id") or "") in generic_step_ids
        ]
        file_contents = await _fetch_dockerfile_prompt_files(generic_file_refs)
        for attempt in range(2):
            try:
                raw_payload = await _generate_dockerfiles_payload_with_llm(
                    steps=generic_steps,
                    pipeline_graph=pipeline_graph or {},
                    file_contents=file_contents,
                    llm_config=resolved_config,
                    validation_errors=validation_errors,
                )
                normalized = _normalize_llm_dockerfile_payload(
                    raw_payload,
                    generic_steps,
                )
                generic_dockerfiles = normalized["dockerfiles"]
                validate_dockerfile_artifacts(
                    generic_dockerfiles,
                    [step["flow_id"] for step in generic_steps],
                    generic_steps,
                )
                break
            except DeploymentArtifactValidationError as exc:
                validation_errors = exc.errors
            except ValueError as exc:
                validation_errors = [str(exc)]
            if validation_errors:
                print(
                    "[deployment_agents.py] LLM Dockerfile guardrail validation failed "
                    f"on attempt {attempt + 1}: {validation_errors}"
                )
        else:
            raise DeploymentArtifactValidationError(
                "Dockerfile guardrail validation failed",
                validation_errors,
            )

    step_order = {
        str(step["flow_id"]): index
        for index, step in enumerate(steps)
    }
    dockerfiles = sorted(
        [*codegen_dockerfiles, *deterministic_dockerfiles, *generic_dockerfiles],
        key=lambda item: step_order.get(str(item.get("flow_id") or ""), len(steps)),
    )
    validate_dockerfile_artifacts(
        dockerfiles,
        [step["flow_id"] for step in steps],
        steps,
    )
    runtime_artifacts = [
        *codegen_runtime_artifacts,
        *[
            bundle.to_dict(include_content=True)
            for bundle in deterministic_bundles
        ],
    ]
    attached_files = await _fetch_attached_deployment_files(file_refs)
    routing_errors = _input_attachment_routing_errors(
        steps,
        pipeline_graph or {},
        attached_files,
    )
    if routing_errors:
        raise DeploymentArtifactValidationError(
            "Node input attachment routing failed",
            routing_errors,
        )
    artifact_payload = {
        "dockerfiles": dockerfiles,
        "runtime_artifacts": runtime_artifacts,
        "deployment_files": _deployment_files_from_artifacts(
            dockerfiles,
            runtime_artifacts,
            attached_files,
        ),
        "guardrails": {
            "valid": True,
            "checks": [
                "current graph node attachments were used as the deployment source",
                "persisted codegen runtime artifacts were reused before Dockerfile fallback",
                "registered node generators bypassed the LLM",
                "generic nodes used the existing guarded LLM path",
                "one validated Dockerfile was produced per executable pipeline step",
            ],
        },
    }

    print("[deployment_agents.py] Deployment artifacts generated and validated.")
    if hasattr(ListDockerfilesResponse, "model_validate"):
        return ListDockerfilesResponse.model_validate(artifact_payload)
    return ListDockerfilesResponse.parse_obj(artifact_payload)


def _deployment_files_from_artifacts(
    dockerfiles: list[dict[str, Any]],
    runtime_artifacts: list[dict[str, Any]],
    attached_files: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    def add_file(
        *,
        flow_id: str,
        filename: str,
        content: Any,
        content_type: str = "text/plain",
        role: str = "runtime",
        encoding: str = "",
    ) -> None:
        clean_filename = str(filename or "").strip()
        if not clean_filename:
            return
        node_dir = _sanitize_fragment(flow_id or "pipeline", "node")
        path = f"nodes/{node_dir}/{clean_filename}"
        if path in seen_paths:
            return
        seen_paths.add(path)
        files.append(
            {
                "path": path,
                "filename": clean_filename,
                "flow_id": str(flow_id or ""),
                "content": str(content or ""),
                "content_type": content_type,
                "role": role,
                **({"encoding": encoding} if encoding else {}),
            }
        )

    for artifact in runtime_artifacts:
        if not isinstance(artifact, dict):
            continue
        flow_id = str(artifact.get("flow_id") or "").strip()
        for file_item in artifact.get("files") or []:
            if not isinstance(file_item, dict):
                continue
            filename = str(file_item.get("filename") or "").strip()
            add_file(
                flow_id=flow_id,
                filename=filename,
                content=file_item.get("content"),
                content_type=str(file_item.get("content_type") or "text/plain"),
                role="dockerfile" if filename.startswith("Dockerfile.") else "runtime",
            )

    for dockerfile in dockerfiles:
        if not isinstance(dockerfile, dict):
            continue
        add_file(
            flow_id=str(dockerfile.get("flow_id") or ""),
            filename=str(dockerfile.get("dockerfile_filename") or ""),
            content=dockerfile.get("content"),
            content_type="text/x-dockerfile",
            role="dockerfile",
        )

    for file_item in attached_files or []:
        if not isinstance(file_item, dict):
            continue
        add_file(
            flow_id=str(file_item.get("flow_id") or file_item.get("step_id") or ""),
            filename=str(file_item.get("filename") or ""),
            content=file_item.get("content"),
            content_type=str(file_item.get("content_type") or "text/plain"),
            role="attachment",
            encoding=str(file_item.get("encoding") or ""),
        )

    return files


async def _fetch_attached_deployment_files(
    file_refs: Optional[list[dict]],
) -> list[dict[str, Any]]:
    retrieved: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entry in file_refs or []:
        if not isinstance(entry, dict):
            continue
        step_id = str(entry.get("step_id") or entry.get("flow_id") or "").strip()
        bucket = str(entry.get("bucket") or f"files-step-id-{step_id}").strip().lower()
        filename = str(entry.get("filename") or "").strip()
        read_bucket = str(entry.get("snapshot_bucket") or bucket).strip().lower()
        read_object = str(entry.get("snapshot_object") or filename).strip()
        key = (step_id, filename)
        if not step_id or not filename or key in seen:
            continue
        seen.add(key)
        try:
            raw_content = await read_minio_object_bytes(read_bucket, read_object)
        except Exception as exc:
            errors.append(
                f"Node {step_id} attachment {filename} could not be read from "
                f"{read_bucket}: {exc}"
            )
            continue
        lower_filename = filename.lower()
        is_runtime_file = (
            lower_filename in RUNTIME_ATTACHMENT_NAMES
            or lower_filename.startswith("dockerfile.")
            or PurePosixPath(lower_filename).suffix in RUNTIME_ATTACHMENT_SUFFIXES
        )
        if not is_runtime_file:
            input_errors = attachment_input_errors(
                filename,
                raw_content[:1024 * 1024],
                size_bytes=len(raw_content),
            )
            if input_errors:
                errors.extend(
                    f"Node {step_id}: {message}"
                    for message in input_errors
                )
                continue
        guessed_content_type = mimetypes.guess_type(filename)[0]
        content_type = str(
            entry.get("content_type")
            or guessed_content_type
            or "application/octet-stream"
        )
        try:
            content = raw_content.decode("utf-8")
            encoding = ""
        except UnicodeDecodeError:
            content = base64.b64encode(raw_content).decode("ascii")
            encoding = "base64"
        retrieved.append(
            {
                "flow_id": step_id,
                "filename": filename,
                "content": content,
                "content_type": content_type,
                **({"encoding": encoding} if encoding else {}),
            }
        )
    if errors:
        raise DeploymentArtifactValidationError(
            "Node attachment retrieval failed",
            errors,
        )
    return retrieved


def _input_attachment_routing_errors(
    steps: list[dict[str, Any]],
    pipeline_graph: dict[str, Any],
    attached_files: list[dict[str, Any]],
) -> list[str]:
    """Catch clear cases where a pipeline input was uploaded to a later node."""
    step_ids = {str(step.get("flow_id") or "") for step in steps}
    incoming = {step_id: 0 for step_id in step_ids}
    for edge in extract_pipeline_edges(pipeline_graph):
        target = str(edge.get("target") or "")
        if target in incoming:
            incoming[target] += 1
    root_ids = {step_id for step_id, count in incoming.items() if count == 0}
    if not root_ids:
        return []

    def is_runtime_file(filename: str) -> bool:
        lower = filename.lower()
        return (
            lower in RUNTIME_ATTACHMENT_NAMES
            or lower.startswith("dockerfile.")
            or PurePosixPath(lower).suffix in RUNTIME_ATTACHMENT_SUFFIXES
        )

    data_files = [
        item
        for item in attached_files
        if isinstance(item, dict)
        and str(item.get("filename") or "").strip()
        and not is_runtime_file(str(item.get("filename") or "").strip())
    ]
    if not data_files:
        return []

    step_by_id = {
        str(step.get("flow_id") or ""): step
        for step in steps
    }
    dynamic_file_search_markers = (
        ".iterdir(",
        ".glob(",
        ".rglob(",
        "glob.glob(",
        "os.listdir(",
        "os.scandir(",
    )
    errors: list[str] = []
    for root_id in sorted(root_ids):
        local_data = [
            item
            for item in data_files
            if str(item.get("flow_id") or "") == root_id
        ]
        if local_data:
            continue
        root_scripts = "\n".join(
            str(item.get("content") or "")
            for item in attached_files
            if str(item.get("flow_id") or "") == root_id
            and str(item.get("filename") or "").lower().endswith(".py")
        ).lower()
        if not root_scripts:
            continue

        for data_file in data_files:
            owner_id = str(data_file.get("flow_id") or "")
            if owner_id in root_ids:
                continue
            filename = str(data_file.get("filename") or "").strip()
            extension = PurePosixPath(filename).suffix.lower()
            exact_name_match = filename.lower() in root_scripts
            dynamic_extension_match = (
                len(data_files) == 1
                and bool(extension)
                and extension in root_scripts
                and any(marker in root_scripts for marker in dynamic_file_search_markers)
            )
            if not exact_name_match and not dynamic_extension_match:
                continue
            root = step_by_id.get(root_id) or {}
            owner = step_by_id.get(owner_id) or {}
            root_label = str(root.get("label") or root_id)
            owner_label = str(owner.get("label") or owner_id)
            errors.append(
                f"{filename} is attached to node {owner_id} ({owner_label}), but "
                f"node {root_id} ({root_label}) appears to read it. Move the input "
                f"file to node {root_id} before generating the bundle."
            )
    return errors


def _codegen_artifact_from_persisted_files(step: dict[str, Any]) -> dict[str, Any] | None:
    files = step.get("files") if isinstance(step.get("files"), list) else []
    filenames = {
        str(item.get("filename") or "").strip()
        for item in files
        if isinstance(item, dict)
    }
    if not any(name.lower().endswith(".py") for name in filenames):
        return None
    runtime_files = [
        item
        for item in files
        if isinstance(item, dict)
        and (
            str(item.get("filename") or "").strip() in RUNTIME_ATTACHMENT_NAMES
            or str(item.get("filename") or "").strip().startswith("Dockerfile.")
            or any(
                str(item.get("filename") or "").strip().lower().endswith(suffix)
                for suffix in RUNTIME_ATTACHMENT_SUFFIXES
            )
        )
    ]
    return {
        "status": "current",
        "generator": CODEGEN_GENERATOR,
        "files": runtime_files,
    }


def _codegen_artifact_for_step(step: dict[str, Any]) -> dict[str, Any] | None:
    artifact = step.get("generated_artifact")
    if not isinstance(artifact, dict):
        return None
    generator = str(artifact.get("generator") or "").strip()
    if generator != CODEGEN_GENERATOR:
        return None
    return artifact


def _codegen_artifact_ready_errors(step: dict[str, Any], artifact: dict[str, Any]) -> list[str]:
    flow_id = str(step.get("flow_id") or "").strip()
    errors: list[str] = []
    status = str(artifact.get("status") or "current").strip().lower()
    if status != "current":
        errors.append(f"Node {flow_id} codegen runtime artifact is {status or 'not current'}.")

    validation_report = artifact.get("validation_report")
    if isinstance(validation_report, dict):
        validation_status = str(validation_report.get("status") or "").strip().lower()
        if validation_status == "invalid":
            errors.append(f"Node {flow_id} codegen runtime artifact is invalid.")

    files = artifact.get("files")
    if not isinstance(files, list) or not files:
        errors.append(f"Node {flow_id} codegen runtime artifact has no persisted files.")
        return errors

    filenames = {
        str(item.get("filename") or "").strip()
        for item in files
        if isinstance(item, dict)
    }
    if not any(name.lower().endswith(".py") for name in filenames):
        errors.append(f"Node {flow_id} codegen runtime artifact is missing a Python entry script.")
    return errors


def _codegen_image_reference(flow_id: str, artifact: dict[str, Any]) -> str:
    image = str(artifact.get("image_reference") or artifact.get("image") or "").strip()
    if image:
        return image
    configuration_hash = str(artifact.get("configuration_hash") or "").strip()
    try:
        from generators.base import node_image_reference

        return node_image_reference(flow_id, configuration_hash, prefix="codegen")
    except Exception:
        return f"inlumen/{_argo_name(flow_id)}:latest"


def _safe_attachment_path(filename: str) -> str:
    path = PurePosixPath(str(filename or "").replace("\\", "/"))
    if not filename or path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise DeploymentArtifactValidationError(
            "Node attachment validation failed",
            [f"Unsafe node attachment path: {filename!r}."],
        )
    return str(path)


def _infer_python_entrypoint(
    retrieved_files: list[dict[str, Any]],
    artifact: dict[str, Any],
    node_manifest: dict[str, Any],
    dockerfile_content: str,
) -> list[str]:
    entrypoint = artifact.get("entrypoint") or node_manifest.get("entrypoint")
    if not isinstance(entrypoint, list) or not all(isinstance(item, str) for item in entrypoint):
        entrypoint = _extract_json_cmd_from_dockerfile(dockerfile_content)
    if entrypoint:
        return entrypoint

    python_scripts = sorted(
        _safe_attachment_path(item["filename"])
        for item in retrieved_files
        if str(item.get("filename") or "").lower().endswith(".py")
    )
    by_basename = {
        PurePosixPath(filename).name.lower(): filename
        for filename in python_scripts
    }
    script = next(
        (by_basename[name] for name in PYTHON_ENTRYPOINT_NAMES if name in by_basename),
        python_scripts[0],
    )
    return ["python", f"/app/{script}"]


def _synthesized_attachment_dockerfile(
    flow_id: str,
    retrieved_files: list[dict[str, Any]],
    entrypoint: list[str],
) -> dict[str, Any]:
    filenames = [
        _safe_attachment_path(item["filename"])
        for item in retrieved_files
        if not str(item.get("filename") or "").startswith("Dockerfile.")
    ]
    lines = [
        "FROM python:3.11-slim",
        "ENV PYTHONUNBUFFERED=1",
        "WORKDIR /app",
    ]
    for filename in filenames:
        lines.append(f"COPY {json.dumps([filename, f'/app/{filename}'])}")
    if any(PurePosixPath(name).name.lower() == "requirements.txt" for name in filenames):
        lines.append("RUN pip install --no-cache-dir -r requirements.txt")
    lines.extend(
        [
            f'LABEL inlumen.flow_id="{flow_id}"',
            f"CMD {json.dumps(entrypoint)}",
        ]
    )
    return {
        "filename": f"Dockerfile.{_sanitize_fragment(flow_id, 'step')}",
        "content": "\n".join(lines) + "\n",
        "generated": True,
    }


async def _read_persisted_codegen_artifact(
    step: dict[str, Any],
    artifact: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    flow_id = str(step.get("flow_id") or "").strip()
    errors = _codegen_artifact_ready_errors(step, artifact)
    if errors:
        raise DeploymentArtifactValidationError(
            "Persisted codegen runtime artifact validation failed",
            errors,
        )

    retrieved_files: list[dict[str, Any]] = []
    for item in artifact.get("files") or []:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        bucket = str(item.get("bucket") or f"files-step-id-{flow_id}").strip().lower()
        read_bucket = str(item.get("snapshot_bucket") or bucket).strip().lower()
        read_object = str(item.get("snapshot_object") or filename).strip()
        if not filename or not read_bucket or not read_object:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            try:
                content = await read_minio_object(read_bucket, read_object)
            except Exception as exc:
                raise DeploymentArtifactValidationError(
                    "Persisted codegen runtime artifact validation failed",
                    [f"Node {flow_id} failed to read {filename} from {read_bucket}: {exc}"],
                ) from exc
        retrieved_files.append(
            {
                "filename": filename,
                "bucket": bucket,
                "content": content,
                "content_type": str(item.get("content_type") or "text/plain"),
            }
        )

    dockerfile = next(
        (item for item in retrieved_files if item["filename"].startswith("Dockerfile.")),
        None,
    )

    node_manifest: dict[str, Any] = {}
    manifest_file = next(
        (item for item in retrieved_files if item["filename"] == "node-manifest.json"),
        None,
    )
    if manifest_file is not None:
        try:
            parsed_manifest = json.loads(str(manifest_file.get("content") or "{}"))
            if isinstance(parsed_manifest, dict):
                node_manifest = parsed_manifest
        except json.JSONDecodeError:
            node_manifest = {}

    validation_report = artifact.get("validation_report")
    if not isinstance(validation_report, dict):
        validation_report = {}
        validation_file = next(
            (item for item in retrieved_files if item["filename"] == "validation-report.json"),
            None,
        )
        if validation_file is not None:
            try:
                parsed_report = json.loads(str(validation_file.get("content") or "{}"))
                if isinstance(parsed_report, dict):
                    validation_report = parsed_report
            except json.JSONDecodeError:
                validation_report = {}

    dockerfile_content = str(dockerfile.get("content") or "") if dockerfile else ""
    entrypoint = _infer_python_entrypoint(
        retrieved_files,
        artifact,
        node_manifest,
        dockerfile_content,
    )
    if dockerfile is None:
        dockerfile = _synthesized_attachment_dockerfile(
            flow_id,
            retrieved_files,
            entrypoint,
        )

    context_files = [
        item["filename"]
        for item in retrieved_files
        if not item["filename"].startswith("Dockerfile.")
    ]
    image_reference = _codegen_image_reference(flow_id, artifact)
    configuration_hash = str(artifact.get("configuration_hash") or "").strip()
    runtime_artifact = {
        "flow_id": flow_id,
        "definition_id": str(step.get("definition_id") or ""),
        "definition_version": step.get("definition_version") or 1,
        "generator": str(artifact.get("generator") or CODEGEN_GENERATOR),
        "generator_version": str(artifact.get("generator_version") or ""),
        "configuration_hash": configuration_hash,
        "image_reference": image_reference,
        "entrypoint": entrypoint,
        "data_contract": (
            artifact.get("data_contract")
            if isinstance(artifact.get("data_contract"), dict)
            else node_manifest.get("data_contract")
            if isinstance(node_manifest.get("data_contract"), dict)
            else {}
        ),
        "files": retrieved_files,
        "manifest": node_manifest,
        "validation_report": validation_report,
    }
    dockerfile_artifact = {
        "dockerfile_filename": dockerfile["filename"],
        "content": dockerfile["content"],
        "flow_id": flow_id,
        "image": image_reference,
        "command": entrypoint,
        "files": context_files,
        "generator": str(artifact.get("generator") or CODEGEN_GENERATOR),
        "configuration_hash": configuration_hash,
        "build_manifest": (
            "node-manifest.json"
            if any(item["filename"] == "node-manifest.json" for item in retrieved_files)
            else None
        ),
        "data_contract": runtime_artifact["data_contract"],
    }
    return runtime_artifact, dockerfile_artifact


async def _fetch_dockerfile_prompt_files(file_refs: Optional[list[dict]]) -> list[dict[str, str]]:
    retrieved: list[dict[str, str]] = []
    for entry in file_refs or []:
        bucket = str(entry.get("bucket") or "").lower()
        filename = str(entry.get("filename") or "")
        step_id = str(entry.get("step_id") or "")
        read_bucket = str(entry.get("snapshot_bucket") or bucket).lower()
        read_object = str(entry.get("snapshot_object") or filename)
        if not bucket or not filename:
            continue
        try:
            content = await read_minio_object(read_bucket, read_object)
        except Exception as exc:
            content = f"[ERROR: {exc}]"
        retrieved.append(
            {
                "step_id": step_id,
                "bucket": bucket,
                "filename": filename,
                "read_bucket": read_bucket,
                "read_object": read_object,
                "content": _truncate_for_prompt(content),
            }
        )
    return retrieved


def _truncate_for_prompt(content: str, max_chars: int = 12000) -> str:
    if len(content) <= max_chars:
        return content
    omitted = len(content) - max_chars
    return f"{content[:max_chars]}\n\n[TRUNCATED {omitted} CHARACTERS]"


def _dockerfile_prompt_context(
    steps: list[dict],
    pipeline_graph: dict,
    file_contents: list[dict[str, str]],
) -> dict[str, Any]:
    prompt_steps = []
    for step in steps:
        flow_id = str(step["flow_id"])
        prompt_steps.append(
            {
                "flow_id": flow_id,
                "expected_dockerfile_filename": f"Dockerfile.{_sanitize_fragment(flow_id, 'step')}",
                "label": step.get("label", ""),
                "description": step.get("description", ""),
                "type": step.get("type", ""),
                "content": step.get("content", ""),
                "endpoint": step.get("endpoint", ""),
                "database": step.get("database", ""),
                "param": step.get("param", {}),
                "files": step.get("files", []),
            }
        )
    return {
        "steps": prompt_steps,
        "edges": pipeline_graph.get("edges", []) if isinstance(pipeline_graph, dict) else [],
        "file_contents": file_contents,
    }


def _dockerfile_system_prompt() -> str:
    return """You generate production-ready Dockerfiles for inLUMEN pipeline steps.
Use natural-language understanding over each step label, description, parameters, attached filenames, and file contents.
Return only one strict JSON object. Do not return markdown, explanations, or code fences."""


def _dockerfile_user_prompt(context: dict[str, Any], validation_errors: list[str]) -> str:
    repair = ""
    if validation_errors:
        repair = (
            "\nThe previous JSON failed validation. Fix all of these issues in the new JSON:\n"
            + json.dumps(validation_errors, indent=2)
            + "\n"
        )

    return f"""Generate Dockerfile artifacts for every step in this context.

Rules:
- Return exactly this shape: {{"dockerfiles":[{{"dockerfile_filename":"Dockerfile.<step_id>","content":"...","flow_id":"<step_id>","image":"inlumen/<step-name>:latest","command":["..."],"files":["..."]}}],"guardrails":{{"valid":true,"checks":["LLM-generated Dockerfiles validated after generation"]}}}}
- Generate exactly one Dockerfile per step, using each step's expected_dockerfile_filename.
- Dockerfile content must start with FROM, include WORKDIR, copy/add attached files when present, and include CMD or ENTRYPOINT.
- Infer the runtime and install commands from attached files and contents. For example, requirements.txt means install Python requirements, package.json means install npm dependencies, shell scripts need bash/chmod, and notebooks/scripts should use a compatible runtime.
- Use JSON-array form for CMD/ENTRYPOINT where practical, and put the same array in the command field.
- Keep the output plain JSON. The Dockerfile content string must not contain markdown fences.
{repair}
Context JSON:
{json.dumps(context, indent=2)}
"""


async def _generate_dockerfiles_payload_with_llm(
    *,
    steps: list[dict],
    pipeline_graph: dict,
    file_contents: list[dict[str, str]],
    llm_config: LLMConfig,
    validation_errors: list[str],
) -> dict[str, Any]:
    model_client = select_model_client(llm_config, parallel_tool_calls=False)
    context = _dockerfile_prompt_context(steps, pipeline_graph, file_contents)
    create_kwargs: dict[str, Any] = {}

    try:
        result = await model_client.create(
            [
                SystemMessage(content=_dockerfile_system_prompt()),
                UserMessage(content=_dockerfile_user_prompt(context, validation_errors), source="user"),
            ],
            **create_kwargs,
        )
    finally:
        close = getattr(model_client, "close", None)
        if close:
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result

    return _coerce_llm_json_payload(result.content)


def _coerce_llm_json_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, ListDockerfilesResponse):
        if hasattr(content, "model_dump"):
            return content.model_dump()
        return content.dict()
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError(f"LLM returned unsupported Dockerfile payload type: {type(content).__name__}")

    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned invalid Dockerfile JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LLM Dockerfile JSON must be an object.")
    return parsed


def _normalize_llm_dockerfile_payload(payload: dict[str, Any], steps: list[dict]) -> dict[str, Any]:
    dockerfiles = payload.get("dockerfiles")
    if not isinstance(dockerfiles, list):
        raise ValueError("LLM Dockerfile JSON must contain a dockerfiles array.")

    step_by_id = {str(step["flow_id"]): step for step in steps}
    normalized = []
    for item in dockerfiles:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        elif hasattr(item, "dict"):
            item = item.dict()
        if not isinstance(item, dict):
            normalized.append(item)
            continue

        flow_id = str(item.get("flow_id") or item.get("step_id") or "").strip()
        if not flow_id:
            match = re.match(
                r"^Dockerfile\.([A-Za-z0-9][A-Za-z0-9_.-]*)$",
                str(item.get("dockerfile_filename") or ""),
            )
            flow_id = match.group(1) if match else ""
        step = step_by_id.get(flow_id, {})
        files = item.get("files")
        if not isinstance(files, list):
            files = [entry["filename"] for entry in step.get("files") or []]
        normalized.append(
            {
                **item,
                "flow_id": flow_id,
                "dockerfile_filename": str(item.get("dockerfile_filename") or ""),
                "content": str(item.get("content") or ""),
                "image": str(item.get("image") or f"inlumen/{_argo_name(flow_id)}:latest"),
                "command": item.get("command") if isinstance(item.get("command"), list) else [],
                "files": [str(name) for name in files],
            }
        )

    return {
        "dockerfiles": normalized,
        "guardrails": {
            "valid": True,
            "checks": [
                "LLM generated Dockerfile content from pipeline context and attached files",
                "one Dockerfile per pipeline step",
                "Dockerfiles passed deterministic format guardrails after generation",
            ],
        },
    }


def _minio_codefetch_tool(params: Optional[dict] = None) -> dict:
    """Download code files referenced by the current pipeline steps for deterministic YAML metadata."""
    payload = params or {}
    file_refs = payload.get("files", []) or []
    retrieved = []
    print("[deployment_agents.py] _minio_codefetch_tool called with entries:", file_refs)
    for entry in file_refs:
        bucket = str(entry.get("bucket") or "").lower()
        filename = str(entry.get("filename") or "")
        step_id = str(entry.get("step_id") or "")
        read_bucket = str(entry.get("snapshot_bucket") or bucket).lower()
        read_object = str(entry.get("snapshot_object") or filename)
        if not bucket or not filename:
            continue
        try:
            content = run_async(read_minio_object(read_bucket, read_object))
        except Exception as exc:
            content = f"[ERROR: {exc}]"
        retrieved.append(
            {
                "step_id": step_id,
                "bucket": bucket,
                "filename": filename,
                "read_bucket": read_bucket,
                "read_object": read_object,
                "content": content,
            }
        )
        print(
            f"[deployment_agents.py] _minio_codefetch_tool read {read_object} from {read_bucket} "
            f"(step {step_id}): {'error' if content.startswith('[ERROR') else 'success'}"
        )
    return {"files": retrieved}


def generate_argo_yaml_from_graph(
    pipeline_graph: dict,
    file_refs: list[dict],
    dockerfiles: dict | list[dict] | None = None,
) -> str:
    """Generate Argo YAML deterministically from graph and Dockerfile metadata."""
    file_contents = _minio_codefetch_tool({"files": file_refs})
    dockerfile_payload = dockerfiles or {"dockerfiles": []}
    return _generate_argo_yaml_tool({
        "pipeline_graph": pipeline_graph or {},
        "file_contents": file_contents,
        "dockerfiles": dockerfile_payload,
    })


def _generate_argo_yaml_tool(params: Optional[dict] = None) -> str:
    """Produce the final Argo Workflow YAML from pipeline, code, and Dockerfile metadata."""
    payload = params or {}
    pipeline = payload.get("pipeline_graph") or {}
    file_contents_raw = payload.get("file_contents") or []
    dockerfiles_raw = payload.get("dockerfiles") or []
    file_contents = (
        file_contents_raw.get("files", [])
        if isinstance(file_contents_raw, dict)
        else file_contents_raw
    )
    dockerfiles = (
        dockerfiles_raw.get("dockerfiles", [])
        if isinstance(dockerfiles_raw, dict)
        else dockerfiles_raw
    )
    workflow_text = build_argo_workflow_yaml(pipeline, {"dockerfiles": dockerfiles}, file_contents)
    print("[deployment_agents.py] _generate_argo_yaml_tool produced deterministic Argo workflow.")
    return workflow_text
