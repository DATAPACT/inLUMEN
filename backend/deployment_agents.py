import ast
import hashlib
import json
import re
from typing import Any, Optional

from connector_catalog import require_supported_connector

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
from model_plans import (
    infer_implementation_plan_from_python_source,
    unresolved_model_plan_errors_from_python_source,
)
from node_parameters import normalize_secret_param_keys

CODEGEN_GENERATOR = "inlumen-codegen-service"
ATTACHED_RUNTIME_GENERATOR = "inlumen-attached-runtime"
MANAGED_ADAPTER_GENERATOR = "inlumen-managed-adapter"
CONTROL_FLOW_GENERATOR = "inlumen-control-flow"


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
        # Explicit connector boundaries are rebuilt from the current connector
        # specification so stale generated artifacts cannot override them.
        # Custom boundaries with an attached main.py are user-owned runtimes
        # and follow the same contract as Tasks.
        if _is_managed_adapter(step):
            runtime_artifact, dockerfile = _managed_adapter_runtime(step)
            codegen_runtime_artifacts.append(runtime_artifact)
            codegen_dockerfiles.append(dockerfile)
            continue

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
                # A previous codegen validation may have persisted an invalid
                # wrapper around otherwise valid user-owned files.  Prefer the
                # current uploaded Task package in that case; old codegen state
                # must not block arbitrary ``main.py`` tasks from exporting.
                if not _has_attached_python_entrypoint(step):
                    artifact_errors.extend(exc.errors)
                    continue
                try:
                    runtime_artifact, dockerfile = await _read_attached_python_runtime(
                        step
                    )
                except DeploymentArtifactValidationError as attached_exc:
                    artifact_errors.extend([
                        *exc.errors,
                        *attached_exc.errors,
                    ])
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

        # Flow nodes are orchestration components, not user Tasks.  They do
        # not require an uploaded main.py; provide the small deterministic
        # pass-through runtime used by the filesystem hand-off instead.
        if _is_control_flow_step(step):
            runtime_artifact, dockerfile = _control_flow_runtime(step)
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
    kind = str(step.get("type") or "").strip().lower()
    if kind not in {
        "source",
        "destination",
    }:
        return False

    # Custom boundaries are intentionally extensible.  If the user attached
    # main.py, preserve that runtime exactly as we do for Tasks.  Explicit
    # Database/Object Storage/etc. selections remain platform-owned adapters,
    # so stale generated code cannot silently bypass the configured connector.
    template = str(
        step.get("template")
        or step.get("template_label")
        or "Custom"
    ).strip().lower()
    if template in {"", "custom"} and _has_attached_python_entrypoint(step):
        generated_artifact = step.get("generated_artifact")
        provenance = (
            generated_artifact.get("provenance")
            if isinstance(generated_artifact, dict)
            else None
        )
        # AI-generated connector artifacts remain platform-managed.  A user
        # edit explicitly marked user_modified (or a standalone uploaded
        # main.py) is the opt-in for a custom boundary runtime.
        if not isinstance(generated_artifact, dict) or not generated_artifact or (
            isinstance(provenance, dict) and provenance.get("user_modified") is True
        ):
            return False
    return True


def _is_control_flow_step(step: dict[str, Any]) -> bool:
    return str(step.get("type") or "").strip().lower() == "flow"


def _deterministic_python_dockerfile(
    flow_id: str,
    filenames: list[str],
    *,
    base_image: str = "python:3.11-slim",
    entrypoint: Optional[list[str]] = None,
    system_packages: Optional[list[str]] = None,
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
    command = entrypoint or ["python", "/app/main.py"]
    apt_lines = []
    if system_packages:
        apt_lines = [
            "RUN apt-get update && apt-get install -y --no-install-recommends \\",
            *[f"    {package} \\" for package in system_packages],
            "    && rm -rf /var/lib/apt/lists/*",
        ]
    content = "\n".join(
        [
            "# syntax=docker/dockerfile:1.7",
            f"FROM {base_image or 'python:3.11-slim'}",
            "ENV PYTHONUNBUFFERED=1",
            *apt_lines,
            "WORKDIR /app",
            *copy_lines,
            *install_lines,
            f"CMD {json.dumps(command)}",
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
        filename = str(
            entry.get("filename") or entry.get("name") or f"input-{{index + 1}}"
        ).replace("\\\\", "/").lstrip("/")
        if not filename or ".." in Path(filename).parts:
            raise ValueError(f"Source adapter received unsafe filename: {{filename!r}}")
        source_path = Path(str(entry.get("path") or filename))
        if not source_path.is_absolute():
            source_path = Path(os.environ["INLUMEN_INPUT_MANIFEST"]).parent / source_path
        if not source_path.is_file():
            raise FileNotFoundError(f"Source adapter input does not exist: {{source_path}}")
        resolved.append((entry, filename, source_path))

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
    input_dir = Path(os.environ["PIPELINE_INPUT_DIR"])
    output_dir = Path(os.environ["PIPELINE_OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = [{{
        "filename": path.relative_to(input_dir).as_posix(),
        "path": str(path),
    }} for path in sorted(input_dir.rglob("*")) if path.is_file()]
    if ADAPTER_SPEC["kind"] == "source":
        _source_outputs(entries, output_dir)
    else:
        _destination_outputs(entries, output_dir)


if __name__ == "__main__":
    main()
'''


def _managed_adapter_main_source_v2(adapter_spec: dict[str, Any]) -> str:
    """Return the runtime for database extraction and object-store delivery."""
    embedded_spec = json.dumps(json.dumps(adapter_spec, sort_keys=True))
    source = '''import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ADAPTER_SPEC = json.loads(__SPEC__)


def _parameter(name, default=""):
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).upper().strip("_")
    for env_name in (f"PIPELINE_PARAM_{normalized}", f"INLUMEN_PARAM_{normalized}", normalized):
        value = os.getenv(env_name)
        if value not in (None, ""):
            return value
    value = (ADAPTER_SPEC.get("parameters") or {}).get(name)
    if value in (None, ""):
        value = (ADAPTER_SPEC.get("settings") or {}).get(name)
    return default if value in (None, "") else value


def _entries(input_dir):
    if not input_dir.is_dir():
        return []
    return [
        {"filename": path.relative_to(input_dir).as_posix(), "path": str(path)}
        for path in sorted(input_dir.rglob("*"))
        if path.is_file() and path.name not in {"input_manifest.json", "output_manifest.json", ".gitkeep"}
    ]


def _port_directory(output_dir, direction="outputs"):
    ports = (ADAPTER_SPEC.get("ports") or {}).get(direction) or []
    port = ports[0] if isinstance(ports[0], dict) else {}
    port_id = str(port.get("id") or port.get("name") or "data")
    port_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", port_id).strip("-") or "data"
    return output_dir / port_id


def _copy_source_files(entries, output_dir):
    port_dir = _port_directory(output_dir)
    for entry in entries:
        filename = str(entry["filename"]).replace("\\\\", "/").lstrip("/")
        if not filename or ".." in Path(filename).parts:
            raise ValueError(f"Source adapter received unsafe filename: {filename!r}")
        destination = port_dir / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry["path"], destination)


def _database_source(output_dir):
    connection_url = _parameter("connection_url") or os.getenv("DATABASE_URL", "")
    query = _parameter("query")
    if not connection_url:
        raise RuntimeError("Database Source requires the connection_url parameter.")
    if not query:
        raise RuntimeError("Database Source requires the query parameter.")

    import psycopg

    output_format = str(_parameter("output_format", "csv")).strip().lower()
    if output_format not in {"csv", "parquet"}:
        raise RuntimeError("Database Source output_format must be csv or parquet.")
    default_filename = f"database_rows.{output_format}"
    filename = Path(str(_parameter("output_filename", default_filename))).name
    port_dir = _port_directory(output_dir)
    port_dir.mkdir(parents=True, exist_ok=True)
    output_path = port_dir / filename
    with psycopg.connect(connection_url, connect_timeout=int(_parameter("connect_timeout", "10"))) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            if cursor.description is None:
                raise RuntimeError("Database query did not return rows.")
            columns = [getattr(column, "name", column[0]) for column in cursor.description]
            if output_format == "parquet":
                import pyarrow as pa
                import pyarrow.parquet as parquet
                rows = cursor.fetchall()
                table = pa.Table.from_pylist([
                    dict(zip(columns, row)) for row in rows
                ])
                parquet.write_table(table, output_path)
                row_count = len(rows)
            else:
                row_count = 0
                with output_path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(columns)
                    while True:
                        rows = cursor.fetchmany(1000)
                        if not rows:
                            break
                        writer.writerows(rows)
                        row_count += len(rows)

    manifest_path = output_dir / "output_manifest.json"
    manifest = {
        "schema_version": "inlumen.data-artifact@1",
        "name": output_path.stem,
        "kind": "table",
        "format": output_format,
        "filename": str(output_path.relative_to(output_dir)),
        "columns": columns,
        "row_count": row_count,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\\n", encoding="utf-8")


def _object_storage_source(output_dir):
    bucket = str(_parameter("bucket"))
    if not bucket:
        raise RuntimeError("Object Storage Source requires the bucket parameter.")
    prefix = str(_parameter("prefix", "")).strip("/")
    object_name = str(_parameter("object_name", "")).strip("/")
    client = _storage_client()
    if object_name:
        object_names = [object_name]
    else:
        object_names = [
            item.object_name
            for item in client.list_objects(bucket, prefix=prefix, recursive=True)
            if not item.is_dir
        ]
    if not object_names:
        raise RuntimeError(f"No objects found in bucket {bucket!r} for prefix {prefix!r}.")

    port_dir = _port_directory(output_dir)
    downloaded = []
    for object_key in object_names:
        relative = object_key
        if prefix and relative.startswith(prefix + "/"):
            relative = relative[len(prefix) + 1:]
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"Object Storage Source received unsafe object key: {object_key!r}")
        destination = port_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        client.fget_object(bucket, object_key, str(destination))
        downloaded.append({
            "filename": str(destination.relative_to(output_dir)),
            "bucket": bucket,
            "object": object_key,
        })

    (output_dir / "output_manifest.json").write_text(
        json.dumps({
            "schema_version": "inlumen.data-artifact@1",
            "kind": "object-set",
            "bucket": bucket,
            "prefix": prefix,
            "objects": downloaded,
        }, indent=2) + "\\n",
        encoding="utf-8",
    )


def _storage_client():
    endpoint = str(_parameter("endpoint", "minio:9000"))
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    host = parsed.netloc or parsed.path
    secure = str(_parameter("secure", "true" if parsed.scheme == "https" else "false")).lower() in {"1", "true", "yes"}
    from minio import Minio
    return Minio(host, access_key=str(_parameter("access_key", "minio-datapact")), secret_key=str(_parameter("secret_key", "minio-datapact")), secure=secure)


def _api_headers(content_type=""):
    configured = _parameter("headers", "{}")
    try:
        headers = json.loads(configured) if isinstance(configured, str) else dict(configured)
    except (TypeError, ValueError):
        raise RuntimeError("REST API headers must be a JSON object.")
    if not isinstance(headers, dict):
        raise RuntimeError("REST API headers must be a JSON object.")
    headers = {str(key): str(value) for key, value in headers.items()}
    api_key = str(_parameter("api_key") or os.getenv("API_KEY", ""))
    if api_key:
        header_name = str(_parameter("api_key_header", "Authorization"))
        prefix = str(_parameter("api_key_prefix", "Bearer")).strip()
        headers.setdefault(header_name, f"{prefix} {api_key}".strip())
    if content_type:
        headers.setdefault("Content-Type", content_type)
    return headers


def _api_endpoint():
    endpoint = str(
        _parameter("url")
        or _parameter("endpoint")
        or os.getenv("API_ENDPOINT", "")
    ).strip()
    if not endpoint:
        raise RuntimeError(
            "REST API connector requires url/endpoint or the API_ENDPOINT environment variable."
        )
    if urlparse(endpoint).scheme not in {"http", "https"}:
        raise RuntimeError("REST API endpoint must use http or https.")
    return endpoint


def _rest_api_source(output_dir):
    method = str(_parameter("method", "GET")).upper()
    request = Request(
        _api_endpoint(),
        method=method,
        headers=_api_headers(),
    )
    with urlopen(request, timeout=float(_parameter("timeout", "30"))) as response:
        payload = response.read()
        content_type = response.headers.get_content_type()
    extension = {
        "application/json": "json",
        "text/csv": "csv",
        "text/plain": "txt",
    }.get(content_type, "bin")
    filename = Path(str(_parameter("output_filename", f"response.{extension}"))).name
    port_dir = _port_directory(output_dir)
    port_dir.mkdir(parents=True, exist_ok=True)
    output_path = port_dir / filename
    output_path.write_bytes(payload)
    (output_dir / "output_manifest.json").write_text(
        json.dumps({
            "schema_version": "inlumen.data-artifact@1",
            "kind": "api-response",
            "filename": str(output_path.relative_to(output_dir)),
            "content_type": content_type,
            "size_bytes": len(payload),
        }, indent=2) + "\\n",
        encoding="utf-8",
    )


def _rest_api_destination(entries, output_dir):
    endpoint = _api_endpoint()
    method = str(_parameter("method", "POST")).upper()
    delivered = []
    for entry in entries:
        content_type = mimetypes.guess_type(str(entry["filename"]))[0] or "application/octet-stream"
        request = Request(
            endpoint,
            data=Path(entry["path"]).read_bytes(),
            method=method,
            headers=_api_headers(content_type),
        )
        with urlopen(request, timeout=float(_parameter("timeout", "30"))) as response:
            response.read()
            delivered.append({
                "filename": entry["filename"],
                "status": response.status,
            })
    (output_dir / "delivery-receipt.json").write_text(
        json.dumps({"endpoint": endpoint, "delivered": delivered}, indent=2) + "\\n",
        encoding="utf-8",
    )


def _destination_outputs(entries, output_dir):
    bucket = str(_parameter("bucket"))
    if not bucket:
        raise RuntimeError("Object Storage Destination requires the bucket parameter.")
    prefix = str(_parameter("prefix", "")).strip("/")
    object_name = str(_parameter("object_name", ""))
    client = _storage_client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    uploaded = []
    for entry in entries:
        filename = str(entry["filename"]).replace("\\\\", "/").lstrip("/")
        target = object_name if object_name and len(entries) == 1 else filename
        key = "/".join(part for part in (prefix, target) if part)
        client.fput_object(bucket, key, entry["path"], content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")
        uploaded.append({"filename": filename, "bucket": bucket, "object": key})

    receipt_path = output_dir / "delivery-receipt.json"
    receipt_path.write_text(json.dumps({"bucket": bucket, "uploaded": uploaded}, indent=2) + "\\n", encoding="utf-8")


def _generic_destination_outputs(entries, output_dir):
    """Keep a connector-neutral copy for Custom/File/Folder destinations."""
    copied = []
    requested_filename = str(_parameter("filename", "")).strip()
    for index, entry in enumerate(entries):
        filename = str(entry.get("filename") or "").replace("\\\\", "/").lstrip("/")
        if not filename or Path(filename).is_absolute() or ".." in Path(filename).parts:
            raise ValueError(f"Destination received unsafe filename: {filename!r}")
        target = requested_filename if requested_filename and len(entries) == 1 else filename
        target_path = output_dir / target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry["path"], target_path)
        copied.append({
            "filename": str(target_path.relative_to(output_dir)),
            "source": filename,
        })
    (output_dir / "delivery-receipt.json").write_text(
        json.dumps({"mode": "filesystem", "copied": copied}, indent=2) + "\\n",
        encoding="utf-8",
    )


def _is_filesystem_destination_template(template):
    """Accept current and legacy local-output labels without weakening connector routing."""
    normalized = " ".join(str(template or "").strip().lower().split())
    if normalized in {
        "custom",
        "file",
        "file output",
        "folder",
        "folder output",
        "json",
        "json output",
        "structured json",
        "structured object",
        "report",
        "report output",
    }:
        return True
    if normalized.endswith(" output"):
        format_name = normalized[: -len(" output")].strip()
        return format_name in {"csv", "parquet", "text", "binary"}
    return False


def main():
    input_dir = Path(os.environ.get("PIPELINE_INPUT_DIR", "."))
    output_dir = Path(os.environ["PIPELINE_OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)
    kind = str(ADAPTER_SPEC.get("kind") or "")
    template = str(ADAPTER_SPEC.get("template") or "").strip().lower()
    if kind == "source" and template == "database":
        _database_source(output_dir)
    elif kind == "source" and template == "object storage":
        _object_storage_source(output_dir)
    elif kind == "source" and template == "rest api":
        _rest_api_source(output_dir)
    elif kind == "source":
        _copy_source_files(_entries(input_dir), output_dir)
    elif kind == "destination" and template == "object storage":
        _destination_outputs(_entries(input_dir), output_dir)
    elif kind == "destination" and template == "rest api":
        _rest_api_destination(_entries(input_dir), output_dir)
    elif kind == "destination" and _is_filesystem_destination_template(template):
        _generic_destination_outputs(_entries(input_dir), output_dir)
    else:
        raise RuntimeError(
            f"No managed boundary adapter is registered for {kind}/{template}."
        )


if __name__ == "__main__":
    main()
'''.replace("__SPEC__", embedded_spec)
    return source


def _managed_adapter_runtime(
    step: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the deterministic runtime owned by inLumen for graph boundaries."""
    flow_id = str(step.get("flow_id") or "").strip()
    raw_parameters = (
        dict(step.get("param"))
        if isinstance(step.get("param"), dict)
        else {}
    )
    secret_parameters = set(
        normalize_secret_param_keys(step.get("secret_params"), raw_parameters)
    )
    adapter_parameters = {
        key: value for key, value in raw_parameters.items() if key not in secret_parameters
    }
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
        "ports": step.get("ports") if isinstance(step.get("ports"), dict) else {},
        "parameters": adapter_parameters,
        "settings": adapter_settings,
    }
    require_supported_connector(adapter_spec["kind"], adapter_spec["template"])
    main_content = _managed_adapter_main_source_v2(adapter_spec)
    adapter_kind = adapter_spec["kind"]
    adapter_template = adapter_spec["template"].strip().lower()
    runtime_requirements = []
    if (
        adapter_kind == "source"
        and adapter_template == "object storage"
    ) or (
        adapter_kind == "destination"
        and adapter_template == "object storage"
    ):
        runtime_requirements.append("minio>=7.2,<8")
    if adapter_kind == "source" and adapter_template == "database":
        runtime_requirements.append("psycopg[binary]>=3.2,<4")
        if str(adapter_parameters.get("output_format") or "csv").strip().lower() == "parquet":
            runtime_requirements.append("pyarrow>=18,<23")
    data_outputs = []
    if adapter_kind == "source" and adapter_template == "database":
        output_ports = adapter_spec.get("ports", {}).get("outputs") or []
        output_port = output_ports[0] if output_ports and isinstance(output_ports[0], dict) else {}
        output_port_id = str(output_port.get("id") or output_port.get("name") or "data")
        output_format = str(adapter_parameters.get("output_format") or "csv").strip().lower()
        output_filename = str(
            adapter_parameters.get("output_filename")
            or f"database_rows.{output_format}"
        )
        data_outputs.append({
            "name": "database_rows",
            "port": output_port_id,
            "filename": f"{output_port_id}/{output_filename}",
            "kind": "table",
            "format": output_format,
            "description": "Rows materialized by the configured read-only database query.",
        })
    elif adapter_kind == "source" and adapter_template == "object storage":
        output_ports = adapter_spec.get("ports", {}).get("outputs") or []
        output_port = output_ports[0] if output_ports and isinstance(output_ports[0], dict) else {}
        output_port_id = str(output_port.get("id") or output_port.get("name") or "data")
        data_outputs.append({
            "name": "objects",
            "port": output_port_id,
            "kind": "object-set",
            "format": "original",
            "description": "Objects downloaded from the configured S3-compatible location.",
        })
    runtime_environment = []
    if adapter_template == "rest api":
        runtime_environment.extend(
            [
                {
                    "name": "API_ENDPOINT",
                    "required": not bool(
                        adapter_parameters.get("url")
                        or adapter_parameters.get("endpoint")
                        or adapter_settings.get("endpoint")
                    ),
                    "secret": False,
                    "description": "REST endpoint fallback when no connector URL is configured.",
                },
                {
                    "name": "API_KEY",
                    "required": False,
                    "secret": True,
                    "description": "Optional credential for secured APIs; omit for an open API.",
                },
            ]
        )
    elif adapter_kind == "source" and adapter_template == "database":
        runtime_environment.append(
            {
                "name": "DATABASE_URL",
                "required": not bool(adapter_parameters.get("connection_url")),
                "secret": True,
                "description": "Database connection fallback.",
            }
        )
    node_manifest = {
        "schema_version": "inlumen.node-manifest@1",
        "flow_id": flow_id,
        "entrypoint": ["python", "/app/main.py"],
        "data_contract": {
            "inputs": [] if adapter_kind == "source" else [{"name": "input_artifacts", "kind": "artifact"}],
            "outputs": data_outputs,
        },
        "adapter": adapter_spec,
        "runtime_environment": runtime_environment,
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
            "content": "\n".join(runtime_requirements) + ("\n" if runtime_requirements else ""),
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
        entrypoint=["python", "/app/main.py"],
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


def _control_flow_main_source(flow_spec: dict[str, Any]) -> str:
    """Return the self-contained runtime for an engine-neutral Flow node."""
    embedded_spec = json.dumps(flow_spec, sort_keys=True)
    return f'''import json
import os
import shutil
from pathlib import Path


FLOW_SPEC = json.loads({embedded_spec!r})


def _runtime_directories():
    input_dir = os.getenv("PIPELINE_INPUT_DIR")
    output_dir = os.getenv("PIPELINE_OUTPUT_DIR")
    if input_dir and output_dir:
        return Path(input_dir), Path(output_dir)

    # Compatibility with the legacy Dagster launcher.
    input_manifest = os.getenv("INLUMEN_INPUT_MANIFEST")
    return (
        Path(input_manifest).parent if input_manifest else Path.cwd(),
        Path(os.environ["INLUMEN_OUTPUT_DIR"]),
    )


def _copy_inputs(input_dir: Path, output_dir: Path):
    outputs = []
    if not input_dir.is_dir():
        return outputs
    for source in sorted(input_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(input_dir)
        if relative.as_posix() in {{"input_manifest.json", "output_manifest.json"}}:
            continue
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        outputs.append({{
            "name": relative.as_posix(),
            "filename": relative.as_posix(),
            "path": str(target.resolve()),
        }})
    return outputs


def main():
    input_dir, output_dir = _runtime_directories()
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = _copy_inputs(input_dir, output_dir)

    output_manifest = os.getenv("INLUMEN_OUTPUT_MANIFEST")
    if output_manifest:
        manifest_path = Path(output_manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({{"schema_version": "inlumen.output-manifest@1", "outputs": outputs}}, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
'''


def _control_flow_runtime(
    step: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the deterministic runtime owned by inLUMEN for Flow nodes."""
    flow_id = str(step.get("flow_id") or "").strip()
    flow_spec = {
        "kind": "flow",
        "template": str(step.get("template") or "").strip(),
        "label": str(step.get("label") or "").strip(),
        "parameters": dict(step.get("param"))
        if isinstance(step.get("param"), dict)
        else {},
    }
    main_content = _control_flow_main_source(flow_spec)
    ports = step.get("ports") if isinstance(step.get("ports"), dict) else {}
    node_manifest = {
        "schema_version": "inlumen.node-manifest@1",
        "flow_id": flow_id,
        "entrypoint": ["python", "/app/main.py"],
        "data_contract": {
            "inputs": ports.get("inputs") or [],
            "outputs": ports.get("outputs") or [],
        },
        "adapter": flow_spec,
        "source": "inLUMEN deterministic control-flow adapter",
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
        entrypoint=["python", "/app/main.py"],
    )
    configuration_hash = hashlib.sha256(
        json.dumps(
            {"flow_id": flow_id, "flow": flow_spec, "runtime": main_content},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    image = f"inlumen/control-flow-{_sanitize_fragment(flow_id, 'flow')}:{configuration_hash[:12]}"
    runtime_artifact = {
        "flow_id": flow_id,
        "definition_id": str(step.get("definition_id") or ""),
        "definition_version": step.get("definition_version") or 1,
        "generator": CONTROL_FLOW_GENERATOR,
        "generator_version": "1",
        "configuration_hash": configuration_hash,
        "entrypoint": ["python", "/app/main.py"],
        "data_contract": node_manifest["data_contract"],
        "files": runtime_files,
        "manifest": node_manifest,
        "validation_report": {"status": "valid", "errors": [], "warnings": []},
    }
    return runtime_artifact, {
        "dockerfile_filename": dockerfile_filename,
        "content": dockerfile_content,
        "flow_id": flow_id,
        "image": image,
        "command": ["python", "/app/main.py"],
        "files": [item["filename"] for item in runtime_files],
        "generator": CONTROL_FLOW_GENERATOR,
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


def _is_function_style_task(source: object) -> bool:
    """Recognize the small, portable ``run(input, params)`` upload contract."""
    if not isinstance(source, str):
        return False
    try:
        tree = ast.parse(source, filename="main.py")
    except SyntaxError:
        return False
    return any(
        isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == "run"
        for statement in tree.body
    )


def _cli_task_contract(source: object) -> dict[str, Any]:
    """Recognize a conventional CLI task and its portable I/O options."""
    if not isinstance(source, str):
        return {}
    try:
        tree = ast.parse(source, filename="main.py")
    except SyntaxError:
        return {}

    options: set[str] = set()
    path_variables: set[str] = set()
    output_is_directory = False
    output_is_file = False

    def is_args_option(value: ast.AST, option: str) -> bool:
        return (
            isinstance(value, ast.Attribute)
            and value.attr == option
            and isinstance(value.value, ast.Name)
            and value.value.id == "args"
        )

    def is_path_of_output(value: ast.AST) -> bool:
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "Path"
            and bool(value.args)
            and is_args_option(value.args[0], "output")
        )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    if argument.value.startswith("--"):
                        options.add(argument.value)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is not None and is_path_of_output(value):
                for target in targets:
                    if isinstance(target, ast.Name):
                        path_variables.add(target.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            is_output_path = is_path_of_output(target) or (
                isinstance(target, ast.Name) and target.id in path_variables
            )
            if is_output_path and node.func.attr == "mkdir":
                output_is_directory = True
            if is_output_path and node.func.attr in {"write_text", "write_bytes", "open"}:
                output_is_file = True

    if not {"--input", "--output"}.issubset(options):
        return {}
    return {
        "input_option": "--input",
        "output_option": "--output",
        "options": sorted(options),
        "output_kind": "directory" if output_is_directory or not output_is_file else "file",
    }


def _declared_task_io_contract(runtime_files: list[dict[str, Any]]) -> dict[str, Any]:
    """Read an optional portable I/O override shipped with an uploaded task."""
    for filename in ("inlumen.task.json", "task-io.json"):
        candidate = next(
            (item for item in runtime_files if item.get("filename") == filename),
            None,
        )
        if candidate is None:
            continue
        try:
            value = json.loads(str(candidate.get("content") or ""))
        except json.JSONDecodeError as exc:
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                [f"{filename} is not valid JSON: {exc}"],
            ) from exc
        if not isinstance(value, dict):
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                [f"{filename} must contain an object."],
            )
        return value
    return {}


def _task_io_contract(
    source: object,
    runtime_files: list[dict[str, Any]],
    *,
    declared: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Resolve the one portable I/O contract for an uploaded task package."""
    declared = declared if isinstance(declared, dict) else _declared_task_io_contract(runtime_files)
    adapter = str(
        ((declared.get("execution") or {}).get("adapter"))
        if isinstance(declared.get("execution"), dict)
        else ""
    ).strip().lower()
    cli = _cli_task_contract(source)

    if not adapter and _is_function_style_task(source):
        adapter = "function"
    if not adapter and cli:
        adapter = "cli"
    if not adapter and isinstance(source, str) and all(
        name in source
        for name in ("INLUMEN_INPUT_MANIFEST", "INLUMEN_OUTPUT_MANIFEST")
    ):
        adapter = "manifest"
    # A regular main.py is the canonical Task package. It does not need a
    # marker file or an inferred function/CLI shape: the workspace directories
    # are the complete public contract.
    if not adapter:
        adapter = "filesystem"
    if adapter not in {"filesystem", "function", "cli", "manifest"}:
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            [
                "Unsupported task execution adapter. Use a regular main.py "
                "that reads PIPELINE_INPUT_DIR and writes PIPELINE_OUTPUT_DIR, "
                "or one of the legacy function, CLI, or manifest adapters."
            ],
        )

    execution = {
        "adapter": adapter,
        **(
            {
                "input_option": cli["input_option"],
                "output_option": cli["output_option"],
                "options": cli["options"],
            }
            if adapter == "cli" and cli
            else {}
        ),
    }
    if adapter == "cli" and not cli:
        supplied = declared.get("execution") if isinstance(declared.get("execution"), dict) else {}
        if not all(isinstance(supplied.get(key), str) for key in ("input_option", "output_option")):
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                ["CLI task contract requires execution.input_option and execution.output_option."],
            )
        execution.update({
            "input_option": supplied["input_option"],
            "output_option": supplied["output_option"],
            "options": list(supplied.get("options") or []),
        })

    declared_input = declared.get("input") if isinstance(declared.get("input"), dict) else {}
    declared_output = declared.get("output") if isinstance(declared.get("output"), dict) else {}
    input_delivery = str(declared_input.get("delivery") or (
        "object" if adapter == "function" else "manifest" if adapter == "manifest" else "directory" if adapter == "filesystem" else "auto"
    )).lower()
    output_discovery = str(declared_output.get("discovery") or (
        "return-value" if adapter == "function" else "manifest" if adapter == "manifest" else "scan"
    )).lower()
    output_target = str(declared_output.get("target") or (
        "directory" if not cli else cli["output_kind"]
    )).lower()
    if input_delivery not in {"object", "auto", "file", "directory", "manifest"}:
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            [f"Unsupported task input delivery: {input_delivery}."],
        )
    if output_discovery not in {"return-value", "scan", "manifest"} or output_target not in {"file", "directory"}:
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            ["Task output contract must use discovery return-value, scan, or manifest and target file or directory."],
        )
    required_semantics = {
        "filesystem": ("directory", "scan"),
        "function": ("object", "return-value"),
        "cli": (None, "scan"),
        "manifest": ("manifest", "manifest"),
    }
    required_input, required_output = required_semantics[adapter]
    if required_input is not None and input_delivery != required_input:
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            [
                f"The {adapter} adapter requires input.delivery={required_input}; "
                f"received {input_delivery}."
            ],
        )
    if output_discovery != required_output:
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            [
                f"The {adapter} adapter requires output.discovery={required_output}; "
                f"received {output_discovery}."
            ],
        )
    return {
        "schema_version": "inlumen.task-io@1",
        "execution": execution,
        "input": {"delivery": input_delivery},
        "output": {"discovery": output_discovery, "target": output_target},
    }, "declared" if declared else "inferred"


def _task_capability_contract(
    declared: dict[str, Any],
    io_contract: dict[str, Any],
    inferred_model_plan: dict[str, Any],
) -> dict[str, Any]:
    """Create the authoritative build-time contract for an uploaded Task.

    Code inspection is used only to fill in missing defaults.  Once persisted,
    this contract is what deployment generation validates and consumes.
    """
    dependencies = declared.get("dependencies") if isinstance(declared.get("dependencies"), dict) else {}
    python_dependencies = dependencies.get("python") or []
    system_dependencies = dependencies.get("system") or []
    if not all(isinstance(item, str) and item.strip() for item in python_dependencies):
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            ["capabilities.dependencies.python must be a list of non-empty requirement strings."],
        )
    if not isinstance(system_dependencies, list) or not all(
        isinstance(item, str) and item.strip() for item in system_dependencies
    ):
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            ["capabilities.dependencies.system must be a list of package names."],
        )
    system_dependencies = [
        *system_dependencies,
        *(inferred_model_plan.get("required_system_packages") or []),
    ]
    supported_system_dependencies = {"ffmpeg"}
    unsupported_system_dependencies = sorted(
        {
            str(item).strip()
            for item in system_dependencies
            if str(item).strip() not in supported_system_dependencies
        }
    )
    if unsupported_system_dependencies:
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            [
                "Unsupported portable system dependencies: "
                + ", ".join(unsupported_system_dependencies)
                + ". Use a custom container task for packages outside the reviewed allowlist."
            ],
        )

    raw_models = declared.get("models") or []
    if raw_models and not isinstance(raw_models, list):
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            ["capabilities.models must be a list."],
        )
    models: list[dict[str, Any]] = []
    for index, item in enumerate(raw_models):
        if not isinstance(item, dict):
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                [f"capabilities.models[{index}] must be an object."],
            )
        model_id = str(item.get("model_id") or item.get("id") or "").strip()
        model_revision = str(item.get("model_revision") or item.get("revision") or "").strip()
        runtime = str(item.get("runtime") or "local").strip().lower()
        if runtime not in {"local", "remote"} or not model_id:
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                [f"capabilities.models[{index}] requires model_id and runtime local or remote."],
            )
        if runtime == "local" and not re.fullmatch(r"[0-9a-fA-F]{7,64}", model_revision):
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                [f"Local model {model_id!r} requires a pinned commit revision."],
            )
        credential = str(item.get("credential") or item.get("secret") or "").strip()
        if runtime == "remote" and not credential:
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                [f"Remote model {model_id!r} requires a named credential reference."],
            )
        models.append({
            "model_id": model_id,
            "model_revision": model_revision,
            "runtime": runtime,
            "adapter_id": str(item.get("adapter_id") or "user-declared-model").strip(),
            **({"credential": credential} if credential else {}),
        })
    if not models and inferred_model_plan.get("model_id") and inferred_model_plan.get("model_revision"):
        models.append({
            "model_id": str(inferred_model_plan["model_id"]),
            "model_revision": str(inferred_model_plan["model_revision"]),
            "runtime": "local",
            "adapter_id": str(inferred_model_plan.get("adapter_id") or "inferred-model"),
        })

    resources = declared.get("resources") if isinstance(declared.get("resources"), dict) else {}
    for field in ("cpu", "memory", "gpu", "timeout_seconds"):
        if field in resources and not isinstance(resources[field], (str, int, float)):
            raise DeploymentArtifactValidationError(
                "Attached Python runtime validation failed",
                [f"capabilities.resources.{field} must be a string or number."],
            )
    secrets = declared.get("secrets") or []
    if not isinstance(secrets, list) or not all(isinstance(item, str) and item.strip() for item in secrets):
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            ["capabilities.secrets must be a list of named secret references."],
        )
    secrets = [
        *secrets,
        *(model["credential"] for model in models if model.get("credential")),
    ]
    side_effects = declared.get("side_effects") or []
    if not isinstance(side_effects, list) or not all(isinstance(item, dict) for item in side_effects):
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            ["capabilities.side_effects must be a list of objects."],
        )
    if str(declared.get("mode") or "batch").strip().lower() != "batch":
        raise DeploymentArtifactValidationError(
            "Attached Python runtime validation failed",
            ["Only batch Task execution is supported by the current bundle runtime."],
        )
    return {
        "schema_version": "inlumen.task-capability@1",
        "mode": "batch",
        "execution": io_contract["execution"],
        "input": io_contract["input"],
        "output": io_contract["output"],
        "dependencies": {
            "python": list(dict.fromkeys(item.strip() for item in python_dependencies)),
            "system": list(dict.fromkeys(item.strip() for item in system_dependencies)),
        },
        "models": models,
        "resources": resources,
        "secrets": list(dict.fromkeys(item.strip() for item in secrets)),
        "side_effects": side_effects,
    }


def _merge_python_requirements(content: object, declared: list[str]) -> str:
    """Merge declared package requirements without losing user comments/order."""
    existing = str(content or "")
    lines = existing.splitlines()
    seen = {line.strip().lower() for line in lines if line.strip() and not line.strip().startswith("#")}
    for requirement in declared:
        if requirement.lower() not in seen:
            lines.append(requirement)
            seen.add(requirement.lower())
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def _cli_task_launcher_source(flow_id: str, contract: dict[str, Any]) -> str:
    """Adapt conventional ``--input``/``--output`` scripts to the file ABI."""
    encoded_contract = json.dumps(contract, sort_keys=True)
    return f'''"""Compatibility launcher generated by inLUMEN for CLI tasks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


FLOW_ID = {flow_id!r}
CONTRACT = {encoded_contract}
JSON_EXTENSIONS = {{".json", ".jsonl", ".ndjson"}}
TEXT_EXTENSIONS = {{".txt", ".md", ".csv", ".tsv", ".xml", ".yaml", ".yml"}}
IMAGE_EXTENSIONS = {{".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}}
TABLE_EXTENSIONS = {{".csv", ".tsv", ".parquet", ".xlsx", ".xls"}}


def _load_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {{}}


def _entry_path(entry: dict, manifest_path: Path) -> Path:
    value = Path(str(entry.get("path") or entry.get("filename") or ""))
    return value if value.is_absolute() else manifest_path.parent / value


def _input_path(manifest: dict, manifest_path: Path, output_dir: Path) -> Path:
    entries = manifest.get("inputs") or manifest.get("files") or []
    paths = [
        _entry_path(entry, manifest_path)
        for entry in entries
        if isinstance(entry, dict) and _entry_path(entry, manifest_path).is_file()
    ]
    if not paths:
        raise FileNotFoundError("The task received no readable input artifacts.")
    delivery = CONTRACT.get("input_delivery", "auto")
    if delivery == "manifest":
        return manifest_path
    if delivery == "file":
        if len(paths) != 1:
            raise RuntimeError("This task requires exactly one input file.")
        return paths[0]
    if delivery == "auto" and len(paths) == 1:
        return paths[0]

    staging = output_dir / "_inlumen_inputs"
    staging.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(paths, start=1):
        destination = staging / path.name
        if destination.exists():
            destination = staging / f"{{index}}-{{path.name}}"
        try:
            destination.symlink_to(path)
        except OSError:
            shutil.copy2(path, destination)
    (staging / "input_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return staging


def _kind_and_format(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower().lstrip(".") or "binary"
    if path.suffix.lower() in JSON_EXTENSIONS:
        return "json", suffix
    if path.suffix.lower() in TABLE_EXTENSIONS:
        return "table", suffix
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return "text", suffix
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image", suffix
    return "binary", suffix


def _output_descriptors(output_dir: Path, manifest_path: Path) -> list[dict]:
    descriptors = []
    for path in sorted(output_dir.rglob("*")):
        if (
            not path.is_file()
            or path == manifest_path
            or path.name.startswith("_dagster_")
            or any(part.startswith("_inlumen_") for part in path.relative_to(output_dir).parts)
        ):
            continue
        kind, file_format = _kind_and_format(path)
        relative = path.relative_to(output_dir).as_posix()
        descriptors.append({{
            "name": path.stem,
            "filename": relative,
            "path": str(path),
            "kind": kind,
            "format": file_format,
        }})
    if not descriptors:
        raise RuntimeError("CLI task completed but did not produce any output files.")
    return descriptors


def _parameters() -> dict:
    try:
        value = json.loads(os.getenv("INLUMEN_PARAMS_JSON", "{{}}"))
    except json.JSONDecodeError:
        return {{}}
    return value if isinstance(value, dict) else {{}}


def main() -> None:
    input_manifest_path = Path(os.environ["INLUMEN_INPUT_MANIFEST"])
    output_dir = Path(os.environ["INLUMEN_OUTPUT_DIR"])
    output_manifest_path = Path(os.environ["INLUMEN_OUTPUT_MANIFEST"])
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_input = _input_path(_load_manifest(input_manifest_path), input_manifest_path, output_dir)
    selected_output = output_dir if CONTRACT["output_kind"] == "directory" else output_dir / "result.json"
    command = [
        sys.executable,
        str(Path(__file__).with_name("main.py")),
        CONTRACT["input_option"],
        str(selected_input),
        CONTRACT["output_option"],
        str(selected_output),
    ]
    for key, value in _parameters().items():
        option = "--" + str(key).replace("_", "-")
        if option not in CONTRACT["options"]:
            continue
        if isinstance(value, bool):
            if value:
                command.append(option)
        elif value is not None:
            command.extend([option, str(value)])
    subprocess.run(command, check=True)
    outputs = _output_descriptors(output_dir, output_manifest_path)
    output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    output_manifest_path.write_text(json.dumps({{
        "schema_version": "inlumen.output-manifest@1",
        "flow_id": os.getenv("INLUMEN_FLOW_ID", FLOW_ID),
        "outputs": outputs,
    }}, indent=2) + "\\n", encoding="utf-8")


if __name__ == "__main__":
    main()
'''


def _function_style_task_launcher_source(flow_id: str) -> str:
    """Adapt ``run(input, params)`` uploads to the inLUMEN file-manifest ABI."""
    return f'''"""Compatibility launcher generated by inLUMEN.

It preserves the uploaded task module and adapts its ``run(input, params)``
function to the portable runtime contract used by generated and uploaded tasks.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any


FLOW_ID = {flow_id!r}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {{}}
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {{}}


def _entry_path(entry: dict[str, Any], manifest_path: Path) -> Path:
    value = Path(str(entry.get("path") or entry.get("filename") or ""))
    if value.is_absolute():
        return value
    return manifest_path.parent / value


def _input_value(manifest: dict[str, Any], manifest_path: Path) -> Any:
    entries = manifest.get("inputs") or manifest.get("files") or []
    entries = [entry for entry in entries if isinstance(entry, dict)]
    if not entries:
        return {{}}

    def value_for(entry: dict[str, Any]) -> dict[str, Any]:
        path = _entry_path(entry, manifest_path)
        value = {{
            "path": str(path),
            "file_path": str(path),
            "filename": str(entry.get("filename") or path.name),
        }}
        if str(entry.get("format") or "").lower() == "json" and path.is_file():
            try:
                decoded = _load_json(path)
                value["data"] = decoded
                value.update(decoded)
            except (OSError, json.JSONDecodeError):
                pass
        return value

    values = [value_for(entry) for entry in entries]
    if len(values) == 1:
        return values[0]
    return {{"files": values, "data": values}}


def _load_task_module():
    task_path = Path(__file__).with_name("main.py")
    task_dir = str(task_path.parent)
    if task_dir not in sys.path:
        sys.path.insert(0, task_dir)
    specification = importlib.util.spec_from_file_location(
        "inlumen_uploaded_task", task_path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Could not load uploaded main.py at {{task_path}}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    function = getattr(module, "run", None)
    if not callable(function):
        raise RuntimeError("Uploaded main.py must define callable run(input, params)")
    if inspect.iscoroutinefunction(function):
        raise RuntimeError("Async run functions are not supported; upload a synchronous run(input, params) function")
    return function


def _invoke(function, value: Any, parameters: dict[str, Any]) -> Any:
    signature = inspect.signature(function)
    positional = [
        parameter for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if not positional:
        return function()
    if len(positional) == 1 and not any(
        parameter.kind == parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    ):
        return function(value)
    return function(value, parameters)


def main() -> None:
    input_manifest_path = Path(os.environ["INLUMEN_INPUT_MANIFEST"])
    output_dir = Path(os.environ["INLUMEN_OUTPUT_DIR"])
    output_manifest_path = Path(os.environ["INLUMEN_OUTPUT_MANIFEST"])
    parameters = json.loads(os.getenv("INLUMEN_PARAMS_JSON", "{{}}") or "{{}}")
    if not isinstance(parameters, dict):
        parameters = {{}}

    output_dir.mkdir(parents=True, exist_ok=True)
    result = _invoke(
        _load_task_module(),
        _input_value(_load_json(input_manifest_path), input_manifest_path),
        parameters,
    )
    result_path = output_dir / "result.json"
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\\n")

    manifest = {{
        "schema_version": "inlumen.output-manifest@1",
        "flow_id": os.getenv("INLUMEN_FLOW_ID", FLOW_ID),
        "outputs": [{{
            "name": "result",
            "kind": "json",
            "format": "json",
            "filename": result_path.name,
            "path": str(result_path),
        }}],
    }}
    output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with output_manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\\n")


if __name__ == "__main__":
    main()
'''


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

    main_file = next(
        (item for item in runtime_files if item["filename"] == "main.py"),
        {},
    )
    declared_task_contract = _declared_task_io_contract(runtime_files)
    task_io_contract, task_io_origin = _task_io_contract(
        main_file.get("content"),
        runtime_files,
        declared=declared_task_contract,
    )
    task_adapter = task_io_contract["execution"]["adapter"]
    function_style = task_adapter == "function"
    cli_contract = (
        {
            "input_option": task_io_contract["execution"]["input_option"],
            "output_option": task_io_contract["execution"]["output_option"],
            "options": task_io_contract["execution"].get("options") or [],
            "output_kind": task_io_contract["output"]["target"],
            "input_delivery": task_io_contract["input"]["delivery"],
        }
        if task_adapter == "cli"
        else None
    )
    inferred_model_plan = infer_implementation_plan_from_python_source(
        main_file.get("content"),
        parameters=step.get("param"),
    )
    task_capabilities = _task_capability_contract(
        declared_task_contract,
        task_io_contract,
        inferred_model_plan,
    )
    unresolved_model_warnings = unresolved_model_plan_errors_from_python_source(
        main_file.get("content"),
        parameters=step.get("param"),
    )
    requirements_file = next(
        (item for item in runtime_files if item["filename"] == "requirements.txt"),
        None,
    )
    if requirements_file is not None:
        requirements_file["content"] = _merge_python_requirements(
            requirements_file.get("content"),
            task_capabilities["dependencies"]["python"],
        )
    entrypoint = (
        ["python", "/app/launcher.py"]
        if function_style
        else ["python", "/app/cli_launcher.py"]
        if cli_contract
        else ["python", "/app/main.py"]
    )
    if function_style:
        runtime_files.append(
            {
                "filename": "launcher.py",
                "content": _function_style_task_launcher_source(flow_id),
                "content_type": "text/x-python;charset=utf-8",
            }
        )
    elif cli_contract:
        runtime_files.append(
            {
                "filename": "cli_launcher.py",
                "content": _cli_task_launcher_source(flow_id, cli_contract),
                "content_type": "text/x-python;charset=utf-8",
            }
        )

    node_manifest = {
        "schema_version": "inlumen.node-manifest@1",
        "flow_id": flow_id,
        "entrypoint": entrypoint,
        "data_contract": {
            "inputs": fixture_descriptors,
            "outputs": [],
        },
        "capabilities": task_capabilities,
        "model_requirements": task_capabilities["models"],
        **(
            {"implementation_plan": inferred_model_plan}
            if inferred_model_plan
            else {}
        ),
        "source": (
            "user-attached function-style task adapter"
            if function_style
            else "user-attached CLI task adapter"
            if cli_contract
            else "user-attached filesystem-native task"
        ),
        "io_contract": task_io_contract,
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
        entrypoint=entrypoint,
        system_packages=task_capabilities["dependencies"]["system"],
    )
    runtime_artifact = {
        "flow_id": flow_id,
        "definition_id": str(step.get("definition_id") or ""),
        "definition_version": step.get("definition_version") or 1,
        "generator": ATTACHED_RUNTIME_GENERATOR,
        "entrypoint": entrypoint,
        "data_contract": node_manifest["data_contract"],
        "io_contract": task_io_contract,
        "capabilities": task_capabilities,
        "files": runtime_files,
        "manifest": node_manifest,
        "validation_report": {
            "status": "valid",
            "errors": [],
            "warnings": [
                (
                    f"inLUMEN {task_io_origin} task I/O contract: "
                    f"adapter={task_adapter}, input={task_io_contract['input']['delivery']}, "
                    f"output={task_io_contract['output']['discovery']}."
                ),
                *(
                    [
                        "inLUMEN inferred and pinned a local model requirement "
                        "from the uploaded Python source."
                    ]
                    if inferred_model_plan
                    else []
                ),
                *[
                    f"Node {flow_id}: user-managed model detected. "
                    "It will not be prefetched or pinned by inLUMEN; "
                    "ensure the Task can obtain it at runtime."
                    for _warning in unresolved_model_warnings
                    if not task_capabilities["models"]
                ],
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
        "command": entrypoint,
        "files": [item["filename"] for item in runtime_files],
        "generator": ATTACHED_RUNTIME_GENERATOR,
        "configuration_hash": configuration_hash,
        "build_manifest": "node-manifest.json",
        "data_contract": node_manifest["data_contract"],
        "io_contract": task_io_contract,
        "capabilities": task_capabilities,
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
        entrypoint=entrypoint,
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
