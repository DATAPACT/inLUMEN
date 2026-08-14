import hashlib
import json
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from attachment_validation import attachment_input_errors
from artifact_content import encode_artifact_bytes, is_text_artifact
from artifact_contract import classify_artifact
from async_runtime import run_async
from deployment_artifacts import (
    DeploymentArtifactValidationError,
    UV_PINNED_VERSION,
    _argo_name,
    _sanitize_fragment,
    build_argo_workflow_yaml,
    extract_pipeline_steps,
    select_runtime_steps,
    validate_dockerfile_artifacts,
)
from generators.registry import GeneratorRegistry
from minio_gateway import read_minio_object, read_minio_object_bytes

CODEGEN_GENERATOR = "inlumen-codegen-service"
ATTACHED_RUNTIME_GENERATOR = "inlumen-attached-runtime"
MANAGED_ADAPTER_GENERATOR = "inlumen-managed-adapter"


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
    input_files: list[dict[str, Any]] = Field(default_factory=list)
    root_flow_ids: list[str] = Field(default_factory=list)
    guardrails: Optional[GuardrailReport] = None


async def generate_dockerfiles_with_agent(
    filenames: list[str],
    ids: list[str],
    pipeline_graph: Optional[dict] = None,
    file_refs: Optional[list[dict]] = None,
    *,
    require_attached_runtime: bool = False,
) -> ListDockerfilesResponse:
    """Derive deployment Dockerfiles deterministically from reviewed runtime packages."""
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
        # The current graph is authoritative. Global file listings can contain
        # attachments from another pipeline/version with the same numeric id.
        all_steps = graph_steps
        graph_file_refs = [
            dict(file_ref)
            for step in graph_steps
            for file_ref in step.get("files") or []
            if isinstance(file_ref, dict)
        ]
        if graph_file_refs:
            file_refs = graph_file_refs
        else:
            graph_step_ids = {
                str(step.get("flow_id") or "") for step in graph_steps
            }
            file_refs = [
                dict(file_ref)
                for file_ref in file_refs or []
                if isinstance(file_ref, dict)
                and str(
                    file_ref.get("step_id") or file_ref.get("flow_id") or ""
                )
                in graph_step_ids
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
    artifact_errors: list[str] = []
    for step in steps:
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

        if _has_attached_python_entrypoint(step):
            try:
                runtime_artifact, dockerfile = await _read_attached_python_runtime(step)
            except DeploymentArtifactValidationError as exc:
                artifact_errors.extend(exc.errors)
                continue
            codegen_runtime_artifacts.append(runtime_artifact)
            codegen_dockerfiles.append(dockerfile)
            continue

        if _is_managed_adapter(step):
            runtime_artifact, dockerfile = _managed_adapter_runtime(step)
            codegen_runtime_artifacts.append(runtime_artifact)
            codegen_dockerfiles.append(dockerfile)
            continue

        generator = generator_registry.generator_for_step(step)
        if generator is None:
            artifact_errors.append(
                f"Node {step.get('flow_id') or '<unknown>'} cannot be exported "
                "until it has an attached main.py, a generated runtime bundle, "
                "or a registered deterministic runtime generator."
            )
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
    step_order = {
        str(step["flow_id"]): index
        for index, step in enumerate(steps)
    }
    dockerfiles = sorted(
        [*codegen_dockerfiles, *deterministic_dockerfiles],
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
    input_files = await _read_root_input_files(
        steps=steps,
        pipeline_graph=pipeline_graph or {},
        file_refs=file_refs or [],
        runtime_artifacts=runtime_artifacts,
    )
    artifact_payload = {
        "dockerfiles": dockerfiles,
        "runtime_artifacts": runtime_artifacts,
        "deployment_files": _deployment_files_from_artifacts(
            dockerfiles,
            runtime_artifacts,
        ),
        "input_files": input_files,
        "root_flow_ids": sorted(
            _root_step_ids(steps, pipeline_graph or {}),
        ),
        "guardrails": {
            "valid": True,
            "checks": [
                "persisted runtime artifacts were reused before deployment packaging",
                "all build definitions were derived deterministically without an LLM",
                "one validated Dockerfile was produced per executable pipeline step",
                "root pipeline inputs were packaged separately from generated runtime artifacts",
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
        content_encoding: str = "",
        size_bytes: Any = None,
        sha256: str = "",
    ) -> None:
        clean_filename = str(filename or "").strip()
        if not clean_filename:
            return
        node_dir = _sanitize_fragment(flow_id or "pipeline", "node")
        path = f"nodes/{node_dir}/{clean_filename}"
        if path in seen_paths:
            return
        seen_paths.add(path)
        file_payload = {
            "path": path,
            "filename": clean_filename,
            "flow_id": str(flow_id or ""),
            "content": str(content or ""),
            "content_type": content_type,
            "role": role,
        }
        if content_encoding:
            file_payload["content_encoding"] = content_encoding
        if size_bytes is not None:
            file_payload["size_bytes"] = size_bytes
        if sha256:
            file_payload["sha256"] = sha256
        files.append(file_payload)

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
                content_encoding=str(file_item.get("content_encoding") or ""),
                size_bytes=file_item.get("size_bytes"),
                sha256=str(file_item.get("sha256") or ""),
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

    return files


def _root_step_ids(
    steps: list[dict[str, Any]],
    pipeline_graph: dict[str, Any],
) -> set[str]:
    step_ids = {
        str(step.get("flow_id") or "").strip()
        for step in steps
        if str(step.get("flow_id") or "").strip()
    }
    incoming = {
        str(edge.get("target") or "").strip()
        for edge in (
            pipeline_graph.get("edges")
            if isinstance(pipeline_graph.get("edges"), list)
            else []
        )
        if isinstance(edge, dict)
        and str(edge.get("source") or "").strip() in step_ids
        and str(edge.get("target") or "").strip() in step_ids
    }
    roots = step_ids - incoming
    if roots:
        return roots
    return {str(steps[0].get("flow_id") or "").strip()} if steps else set()


def _step_id_from_file_ref(file_ref: dict[str, Any]) -> str:
    step_id = str(file_ref.get("step_id") or "").strip()
    if step_id:
        return step_id
    bucket = str(file_ref.get("bucket") or "").strip().lower()
    match = re.search(r"files-step-id-(.+)$", bucket)
    return match.group(1).strip() if match else ""


def _root_contract_descriptors(
    root_ids: set[str],
    runtime_artifacts: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    descriptors: dict[tuple[str, str], dict[str, Any]] = {}
    for artifact in runtime_artifacts:
        if not isinstance(artifact, dict):
            continue
        flow_id = str(artifact.get("flow_id") or "").strip()
        if flow_id not in root_ids:
            continue
        contract = (
            artifact.get("data_contract")
            if isinstance(artifact.get("data_contract"), dict)
            else {}
        )
        for descriptor in contract.get("inputs") or []:
            if not isinstance(descriptor, dict):
                continue
            filename = str(
                descriptor.get("filename") or descriptor.get("name") or ""
            ).strip()
            if filename:
                descriptors[(flow_id, filename)] = descriptor
    return descriptors


async def _read_root_input_files(
    *,
    steps: list[dict[str, Any]],
    pipeline_graph: dict[str, Any],
    file_refs: list[dict[str, Any]],
    runtime_artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read user inputs for root nodes without mixing them into runtime code."""
    root_ids = _root_step_ids(steps, pipeline_graph)
    descriptors = _root_contract_descriptors(root_ids, runtime_artifacts)
    runtime_filenames = {
        "main.py",
        "requirements.txt",
        "node-manifest.json",
        "validation-report.json",
    }
    inputs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for file_ref in file_refs:
        if not isinstance(file_ref, dict):
            continue
        flow_id = _step_id_from_file_ref(file_ref)
        filename = str(file_ref.get("filename") or "").strip()
        if not flow_id or flow_id not in root_ids or not filename:
            continue
        if filename in runtime_filenames or filename.startswith("Dockerfile."):
            continue

        descriptor = descriptors.get((flow_id, filename))
        if descriptors and descriptor is None:
            continue
        key = (flow_id, filename)
        if key in seen:
            continue

        bucket = str(
            file_ref.get("snapshot_bucket")
            or file_ref.get("bucket")
            or f"files-step-id-{flow_id}"
        ).strip().lower()
        object_name = str(
            file_ref.get("snapshot_object") or filename
        ).strip()
        if not bucket or not object_name:
            continue
        try:
            raw_content = await read_minio_object_bytes(bucket, object_name)
            input_errors = attachment_input_errors(
                filename,
                raw_content[: 1024 * 1024],
                size_bytes=len(raw_content),
            )
            if input_errors:
                raise DeploymentArtifactValidationError(
                    "Deployment input validation failed",
                    [f"Root node {flow_id}: {message}" for message in input_errors],
                )
            encoded = encode_artifact_bytes(
                raw_content,
                filename=filename,
                content_type=str(file_ref.get("content_type") or ""),
            )
        except DeploymentArtifactValidationError:
            raise
        except Exception as exc:
            raise DeploymentArtifactValidationError(
                "Deployment input packaging failed",
                [
                    f"Root node {flow_id} failed to read required input "
                    f"{filename} from {bucket}: {exc}"
                ],
            ) from exc

        metadata = descriptor if isinstance(descriptor, dict) else {}
        inputs.append(
            {
                "flow_id": flow_id,
                "filename": filename,
                "role": "input",
                **encoded,
                **{
                    field: metadata[field]
                    for field in (
                        "kind",
                        "format",
                        "columns",
                        "required_columns",
                        "schema",
                        "semantic_role",
                        "description",
                    )
                    if metadata.get(field) not in (None, "", [], {})
                },
            }
        )
        seen.add(key)
    return inputs


def _codegen_artifact_from_persisted_files(step: dict[str, Any]) -> dict[str, Any] | None:
    artifact = step.get("generated_artifact")
    provenance = artifact.get("provenance") if isinstance(artifact, dict) else None
    if isinstance(provenance, dict) and provenance.get("user_modified") is True:
        return None
    files = step.get("files") if isinstance(step.get("files"), list) else []
    filenames = {
        str(item.get("filename") or "").strip()
        for item in files
        if isinstance(item, dict)
    }
    has_runtime_files = all(
        required in filenames
        for required in ("main.py", "requirements.txt", "node-manifest.json")
    )
    if not has_runtime_files:
        return None
    return {
        "status": "current",
        "generator": CODEGEN_GENERATOR,
        "files": files,
    }


def _has_attached_python_entrypoint(step: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("filename") or "").strip().lower() == "main.py"
        for item in (step.get("files") or [])
    )


def _is_managed_adapter(step: dict[str, Any]) -> bool:
    return str(step.get("type") or "").strip().lower() in {
        "source",
        "destination",
    }


def _deterministic_python_dockerfile(
    flow_id: str,
    filenames: list[str],
    *,
    base_image: str = "python:3.11-slim",
) -> tuple[str, str]:
    """Create the transient build definition owned by the deployment exporter."""
    runtime_filenames = [
        str(filename).strip()
        for filename in filenames
        if str(filename).strip()
        and not str(filename).strip().startswith("Dockerfile.")
    ]
    copy_lines = [
        f"COPY {json.dumps([filename, '/app/' + filename])}"
        for filename in runtime_filenames
    ]
    install_lines = (
        [
            f"COPY --from=ghcr.io/astral-sh/uv:{UV_PINNED_VERSION} /uv /uvx /bin/",
            "RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system -r requirements.txt",
        ]
        if "requirements.txt" in runtime_filenames
        else []
    )
    content = "\n".join(
        [
            "# syntax=docker/dockerfile:1.7",
            f"FROM {base_image or 'python:3.11-slim'}",
            "ENV PYTHONUNBUFFERED=1",
            "WORKDIR /app",
            *copy_lines,
            *install_lines,
            'CMD ["python", "/app/main.py"]',
            "",
        ]
    )
    return f"Dockerfile.{_sanitize_fragment(flow_id, 'step')}", content


def _managed_adapter_main_source(adapter_spec: dict[str, Any]) -> str:
    embedded_spec = json.dumps(adapter_spec, sort_keys=True)
    return f'''import base64
import json
import os
import shutil
from pathlib import Path


ADAPTER_SPEC = json.loads({embedded_spec!r})


def _read_entries(manifest_path: Path):
    if not manifest_path.is_file():
        return []
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    entries = manifest.get("inputs") or manifest.get("outputs") or manifest.get("files") or []
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def _source_outputs(entries, output_dir: Path):
    resolved = []
    for index, entry in enumerate(entries):
        filename = Path(
            str(entry.get("filename") or entry.get("name") or f"input-{{index + 1}}")
        ).name
        source_path = Path(str(entry.get("path") or filename))
        if not source_path.is_absolute():
            source_path = Path(os.environ["INLUMEN_INPUT_MANIFEST"]).parent / source_path
        if not source_path.is_file():
            raise FileNotFoundError(f"Source adapter input does not exist: {{source_path}}")
        resolved.append((entry, filename, source_path))

    if len(resolved) > 1:
        package = {{"files": []}}
        json_documents = []
        for entry, filename, source_path in resolved:
            kind = str(entry.get("kind") or "binary")
            file_format = str(
                entry.get("format") or source_path.suffix.lstrip(".")
            ).lower()
            package["files"].append({{
                "filename": filename,
                "kind": kind,
                "format": file_format,
                "size_bytes": source_path.stat().st_size,
            }})
            if file_format == "pdf":
                package.setdefault(
                    "pdf_base64",
                    base64.b64encode(source_path.read_bytes()).decode("ascii"),
                )
                package.setdefault("source", filename)
            elif kind == "json" or file_format == "json":
                with source_path.open("r", encoding="utf-8") as handle:
                    value = json.load(handle)
                json_documents.append({{"filename": filename, "data": value}})
                if isinstance(value, dict):
                    for key, nested_value in value.items():
                        package.setdefault(str(key), nested_value)
            elif kind == "text" or file_format in {{"txt", "md"}}:
                package.setdefault("text", source_path.read_text(encoding="utf-8"))
            else:
                package.setdefault(
                    "content_base64",
                    base64.b64encode(source_path.read_bytes()).decode("ascii"),
                )
        if json_documents:
            package["json_documents"] = json_documents
        package_path = output_dir / "source-package.json"
        with package_path.open("w", encoding="utf-8") as handle:
            json.dump(package, handle, indent=2, sort_keys=True)
        return [{{
            "name": "source_package",
            "filename": package_path.name,
            "path": str(package_path.resolve()),
            "kind": "json",
            "format": "json",
        }}]

    outputs = []
    for entry, filename, source_path in resolved:
        destination_path = output_dir / filename
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        output = dict(entry)
        output["name"] = str(output.get("name") or Path(filename).stem)
        output["filename"] = filename
        output["path"] = str(destination_path.resolve())
        outputs.append(output)
    return outputs


def _destination_outputs(entries, output_dir: Path):
    receipt_path = output_dir / "delivery-receipt.json"
    receipt = {{
        "adapter": ADAPTER_SPEC,
        "mode": str(os.getenv("INLUMEN_RUN_MODE") or "test"),
        "received": [
            str(entry.get("filename") or entry.get("name") or "")
            for entry in entries
        ],
    }}
    with receipt_path.open("w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
    return [{{
        "name": "delivery_receipt",
        "filename": receipt_path.name,
        "path": str(receipt_path.resolve()),
        "kind": "json",
        "format": "json",
    }}]


def main():
    input_manifest = Path(os.environ["INLUMEN_INPUT_MANIFEST"])
    output_dir = Path(os.environ["INLUMEN_OUTPUT_DIR"])
    output_manifest = Path(os.environ["INLUMEN_OUTPUT_MANIFEST"])
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = _read_entries(input_manifest)
    if ADAPTER_SPEC["kind"] == "source":
        outputs = _source_outputs(entries, output_dir)
    else:
        outputs = _destination_outputs(entries, output_dir)
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    with output_manifest.open("w", encoding="utf-8") as handle:
        json.dump({{"schema_version": "inlumen.output-manifest@1", "outputs": outputs}}, handle, indent=2)


if __name__ == "__main__":
    main()
'''


def _managed_adapter_runtime(
    step: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the deterministic runtime owned by inLumen for graph boundaries."""
    flow_id = str(step.get("flow_id") or "").strip()
    adapter_parameters = (
        dict(step.get("param"))
        if isinstance(step.get("param"), dict)
        else {}
    )
    adapter_parameters.pop("model_plan", None)
    adapter_settings = {
        key: value
        for key, value in {
            "endpoint": str(step.get("endpoint") or "").strip(),
            "database": str(step.get("database") or "").strip(),
            "advanced": str(step.get("content") or "").strip(),
        }.items()
        if value
    }
    adapter_spec = {
        "kind": str(step.get("type") or "").strip().lower(),
        "template": str(step.get("template") or "").strip(),
        "label": str(step.get("label") or "").strip(),
        "parameters": adapter_parameters,
        "settings": adapter_settings,
    }
    main_content = _managed_adapter_main_source(adapter_spec)
    node_manifest = {
        "schema_version": "inlumen.node-manifest@1",
        "flow_id": flow_id,
        "entrypoint": ["python", "/app/main.py"],
        "data_contract": {"inputs": [], "outputs": []},
        "adapter": adapter_spec,
        "source": "inLumen managed boundary adapter",
    }
    runtime_files = [
        {
            "filename": "main.py",
            "content": main_content,
            "content_type": "text/x-python;charset=utf-8",
        },
        {
            "filename": "requirements.txt",
            "content": "",
            "content_type": "text/plain;charset=utf-8",
        },
        {
            "filename": "node-manifest.json",
            "content": json.dumps(node_manifest, indent=2) + "\n",
            "content_type": "application/json",
        },
    ]
    dockerfile_filename, dockerfile_content = _deterministic_python_dockerfile(
        flow_id,
        [item["filename"] for item in runtime_files],
    )
    configuration_hash = hashlib.sha256(
        json.dumps(
            {
                "flow_id": flow_id,
                "adapter": adapter_spec,
                "runtime": main_content,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    image = (
        f"inlumen/adapter-{_sanitize_fragment(adapter_spec['kind'], 'adapter')}:"
        f"{configuration_hash[:12]}"
    )
    runtime_artifact = {
        "flow_id": flow_id,
        "definition_id": str(step.get("definition_id") or ""),
        "definition_version": step.get("definition_version") or 1,
        "generator": MANAGED_ADAPTER_GENERATOR,
        "configuration_hash": configuration_hash,
        "entrypoint": ["python", "/app/main.py"],
        "data_contract": node_manifest["data_contract"],
        "files": runtime_files,
        "manifest": node_manifest,
        "validation_report": {
            "status": "valid",
            "errors": [],
            "warnings": [],
        },
    }
    return runtime_artifact, {
        "dockerfile_filename": dockerfile_filename,
        "content": dockerfile_content,
        "flow_id": flow_id,
        "image": image,
        "command": ["python", "/app/main.py"],
        "files": [item["filename"] for item in runtime_files],
        "generator": MANAGED_ADAPTER_GENERATOR,
        "configuration_hash": configuration_hash,
        "build_manifest": "node-manifest.json",
        "data_contract": node_manifest["data_contract"],
    }


def _is_attached_runtime_file(file_ref: dict[str, Any]) -> bool:
    filename = str(file_ref.get("filename") or "").strip().lower()
    role = str(file_ref.get("role") or "").strip().lower()
    if role == "data":
        return False
    return role == "code" or filename in {"main.py", "requirements.txt"} or filename.endswith(
        (".py", ".pyi", ".json", ".toml", ".yaml", ".yml", ".sql", ".sh")
    )


async def _read_attached_python_runtime(
    step: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Enrich a user-attached main.py into the canonical runtime package."""
    flow_id = str(step.get("flow_id") or "").strip()
    runtime_files: list[dict[str, Any]] = []
    fixture_descriptors: list[dict[str, Any]] = []

    for file_ref in step.get("files") or []:
        if not isinstance(file_ref, dict):
            continue
        filename = str(file_ref.get("filename") or "").strip()
        if not filename:
            continue
        lower_filename = filename.lower()
        if lower_filename in {"node-manifest.json", "validation-report.json"} or lower_filename.startswith("dockerfile."):
            continue
        if not _is_attached_runtime_file(file_ref):
            fixture_descriptors.append(
                {
                    "name": filename,
                    "filename": filename,
                    "required": True,
                    **classify_artifact(filename),
                }
            )
            continue
        read_bucket = str(
            file_ref.get("snapshot_bucket")
            or file_ref.get("bucket")
            or f"files-step-id-{flow_id}"
        ).strip().lower()
        read_object = str(
            file_ref.get("snapshot_object") or filename
        ).strip()
        try:
            encoded = encode_artifact_bytes(
                await read_minio_object_bytes(read_bucket, read_object),
                filename=filename,
            )
        except Exception as exc:
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                [f"Node {flow_id} failed to read {filename}: {exc}"],
            ) from exc
        runtime_files.append(
            {
                "filename": filename,
                "bucket": str(file_ref.get("bucket") or read_bucket),
                **encoded,
            }
        )

    filenames = {item["filename"] for item in runtime_files}
    if "main.py" not in filenames:
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            [f"Node {flow_id} is missing main.py."],
        )
    if "requirements.txt" not in filenames:
        runtime_files.append(
            {
                "filename": "requirements.txt",
                "content": "",
                "content_type": "text/plain;charset=utf-8",
            }
        )

    node_manifest = {
        "schema_version": "inlumen.node-manifest@1",
        "flow_id": flow_id,
        "entrypoint": ["python", "/app/main.py"],
        "data_contract": {
            "inputs": fixture_descriptors,
            "outputs": [],
        },
        "source": "user-attached runtime package",
    }
    runtime_files.append(
        {
            "filename": "node-manifest.json",
            "content": json.dumps(node_manifest, indent=2) + "\n",
            "content_type": "application/json",
        }
    )
    dockerfile_filename, dockerfile_content = _deterministic_python_dockerfile(
        flow_id,
        [item["filename"] for item in runtime_files],
    )
    runtime_artifact = {
        "flow_id": flow_id,
        "definition_id": str(step.get("definition_id") or ""),
        "definition_version": step.get("definition_version") or 1,
        "generator": ATTACHED_RUNTIME_GENERATOR,
        "entrypoint": ["python", "/app/main.py"],
        "data_contract": node_manifest["data_contract"],
        "files": runtime_files,
        "manifest": node_manifest,
        "validation_report": {
            "status": "valid",
            "errors": [],
            "warnings": [
                "Output filenames are discovered after execution because this attached package did not declare them."
            ],
        },
    }
    configuration_hash = hashlib.sha256(
        json.dumps(
            {
                "flow_id": flow_id,
                "files": [
                    {
                        "filename": item["filename"],
                        "content": item.get("content") or "",
                    }
                    for item in runtime_files
                ],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    image = f"inlumen/attached-{_sanitize_fragment(flow_id, 'step')}:{configuration_hash[:12]}"
    return runtime_artifact, {
        "dockerfile_filename": dockerfile_filename,
        "content": dockerfile_content,
        "flow_id": flow_id,
        "image": image,
        "command": ["python", "/app/main.py"],
        "files": [item["filename"] for item in runtime_files],
        "generator": ATTACHED_RUNTIME_GENERATOR,
        "configuration_hash": configuration_hash,
        "build_manifest": "node-manifest.json",
        "data_contract": node_manifest["data_contract"],
    }


def _codegen_artifact_for_step(step: dict[str, Any]) -> dict[str, Any] | None:
    artifact = step.get("generated_artifact")
    if not isinstance(artifact, dict):
        return None
    generator = str(artifact.get("generator") or "").strip()
    if generator != CODEGEN_GENERATOR:
        return None
    provenance = artifact.get("provenance")
    if isinstance(provenance, dict) and provenance.get("user_modified") is True:
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
    for required in ("main.py", "requirements.txt", "node-manifest.json"):
        if required not in filenames:
            errors.append(f"Node {flow_id} codegen runtime artifact is missing {required}.")
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
        content_type = str(item.get("content_type") or "")
        if not isinstance(content, str):
            try:
                encoded_content = encode_artifact_bytes(
                    await read_minio_object_bytes(read_bucket, read_object),
                    filename=filename,
                    content_type=content_type,
                )
            except Exception as exc:
                raise DeploymentArtifactValidationError(
                    "Persisted codegen runtime artifact validation failed",
                    [f"Node {flow_id} failed to read {filename} from {read_bucket}: {exc}"],
                ) from exc
        else:
            encoded_content = {
                "content": content,
                "content_type": content_type or "text/plain",
                **(
                    {"content_encoding": item.get("content_encoding")}
                    if item.get("content_encoding")
                    else {}
                ),
                **(
                    {"size_bytes": item.get("size_bytes")}
                    if item.get("size_bytes") is not None
                    else {}
                ),
                **({"sha256": item.get("sha256")} if item.get("sha256") else {}),
            }
        retrieved_files.append(
            {
                "filename": filename,
                "bucket": bucket,
                **encoded_content,
            }
        )

    retrieved_files = [
        item for item in retrieved_files
        if not item["filename"].startswith("Dockerfile.")
    ]

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

    entrypoint = artifact.get("entrypoint") or node_manifest.get("entrypoint")
    if not isinstance(entrypoint, list) or not all(isinstance(item, str) for item in entrypoint):
        entrypoint = ["python", "/app/main.py"]

    context_files = [item["filename"] for item in retrieved_files]
    runtime_config = (
        node_manifest.get("runtime")
        if isinstance(node_manifest.get("runtime"), dict)
        else {}
    )
    base_image = str(runtime_config.get("base_image") or "python:3.11-slim").strip()
    dockerfile_filename, dockerfile_content = _deterministic_python_dockerfile(
        flow_id,
        context_files,
        base_image=base_image,
    )
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
        "dockerfile_filename": dockerfile_filename,
        "content": dockerfile_content,
        "flow_id": flow_id,
        "image": image_reference,
        "command": entrypoint,
        "files": context_files,
        "generator": str(artifact.get("generator") or CODEGEN_GENERATOR),
        "configuration_hash": configuration_hash,
        "build_manifest": "node-manifest.json",
        "data_contract": runtime_artifact["data_contract"],
    }
    return runtime_artifact, dockerfile_artifact


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
            content_type = str(entry.get("content_type") or "")
            if is_text_artifact(filename, content_type):
                content = run_async(read_minio_object(read_bucket, read_object))
            else:
                encoded = encode_artifact_bytes(
                    run_async(read_minio_object_bytes(read_bucket, read_object)),
                    filename=filename,
                    content_type=content_type,
                )
                content = (
                    f"[Binary input: filename={filename}, "
                    f"content_type={encoded['content_type']}, "
                    f"size_bytes={encoded['size_bytes']}, "
                    f"sha256={encoded['sha256']}]"
                )
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
