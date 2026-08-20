from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import tarfile
import tempfile
import threading
import wave
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import docker
import httpx
from docker.errors import DockerException, ImageNotFound
from packaging.requirements import InvalidRequirement, Requirement
from requests.exceptions import ReadTimeout

from .schemas import (
    ExpectedArtifact,
    FileDescriptor,
    FileSample,
    GeneratedArtifact,
    ValidationReport,
)

DOCKER_VALIDATION_ENV = "CODEGEN_DOCKER_VALIDATION_ENABLED"
VALIDATION_WORKDIR_ENV = "CODEGEN_VALIDATION_WORKDIR"
DEFAULT_VALIDATION_WORKDIR = "/tmp/inlumen-codegen-workspaces"
INPUT_FILE_BASE_URL_ENV = "CODEGEN_INPUT_FILE_BASE_URL"
INPUT_FILE_MAX_BYTES_ENV = "CODEGEN_INPUT_FILE_MAX_BYTES"
INPUT_FILE_ALLOW_HTTP_ENV = "CODEGEN_INPUT_FILE_ALLOW_INSECURE_HTTP"
DEPENDENCY_INSTALL_TIMEOUT_ENV = "CODEGEN_DEPENDENCY_INSTALL_TIMEOUT_SECONDS"
DEFAULT_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 600
SANDBOX_RUN_LABEL = "inlumen.codegen.run_id"
_ACTIVE_SANDBOX_CONTAINERS: dict[str, set[str]] = {}
_CANCELLED_SANDBOX_RUNS: set[str] = set()
_SANDBOX_LOCK = threading.RLock()


class SandboxUnavailable(RuntimeError):
    pass


class SandboxCancelled(RuntimeError):
    pass


@dataclass
class SandboxNodeRunResult:
    flow_id: str
    validation_report: ValidationReport
    inputs: list[FileDescriptor]
    outputs: list[FileDescriptor]


def docker_validation_enabled() -> bool:
    return os.getenv(DOCKER_VALIDATION_ENV, "").lower() in {"1", "true", "yes", "on"}


def cancel_sandbox_run(run_id: str) -> None:
    """Mark a run cancelled and terminate every registered Docker container."""
    if not run_id:
        return
    with _SANDBOX_LOCK:
        _CANCELLED_SANDBOX_RUNS.add(run_id)
        container_ids = list(_ACTIVE_SANDBOX_CONTAINERS.get(run_id, set()))
    client = None
    try:
        client = docker.from_env()
        containers = {
            container.id: container
            for container in client.containers.list(
                all=True,
                filters={"label": f"{SANDBOX_RUN_LABEL}={run_id}"},
            )
        }
        for container_id in container_ids:
            try:
                containers.setdefault(
                    container_id,
                    client.containers.get(container_id),
                )
            except DockerException:
                pass
        for container in containers.values():
            try:
                container.remove(force=True)
            except DockerException:
                pass
    finally:
        if client is not None:
            client.close()


def _raise_if_sandbox_cancelled(run_id: str | None) -> None:
    if not run_id:
        return
    with _SANDBOX_LOCK:
        if run_id in _CANCELLED_SANDBOX_RUNS:
            raise SandboxCancelled(f"Sandbox work for run {run_id} was cancelled.")


def _register_sandbox_container(run_id: str | None, container) -> None:
    if not run_id:
        return
    with _SANDBOX_LOCK:
        if run_id in _CANCELLED_SANDBOX_RUNS:
            try:
                container.remove(force=True)
            except DockerException:
                pass
            raise SandboxCancelled(f"Sandbox work for run {run_id} was cancelled.")
        _ACTIVE_SANDBOX_CONTAINERS.setdefault(run_id, set()).add(container.id)


def _unregister_sandbox_container(run_id: str | None, container) -> None:
    if not run_id or container is None:
        return
    with _SANDBOX_LOCK:
        containers = _ACTIVE_SANDBOX_CONTAINERS.get(run_id)
        if containers is None:
            return
        containers.discard(container.id)
        if not containers:
            _ACTIVE_SANDBOX_CONTAINERS.pop(run_id, None)


def validate_node_with_docker(
    *,
    flow_id: str,
    artifact: GeneratedArtifact,
    input_files: list[FileDescriptor],
    parameters: dict[str, object] | None = None,
    timeout_seconds: int,
    run_id: str | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> ValidationReport:
    checks = [
        "docker_validation_enabled",
        "docker_build",
        "sample_input_manifest",
        "container_sample_run",
        "output_manifest_contract",
        "output_file_shape",
    ]
    if not docker_validation_enabled():
        return ValidationReport(
            status="not_run",
            checks=checks,
            warnings=[
                f"Docker execution validation skipped; set {DOCKER_VALIDATION_ENV}=true."
            ],
        )

    with validation_workspace(flow_id) as tmp:
        workspace = Path(tmp)
        inputs_dir = workspace / "inputs"
        outputs_dir = workspace / "outputs"
        context_path = workspace / "context.json"
        inputs_dir.mkdir()
        outputs_dir.mkdir()
        context_path.write_text("{}\n", encoding="utf-8")
        write_generated_files(workspace, artifact)
        manifest_path = inputs_dir / "input_manifest.json"
        write_sample_inputs(manifest_path, inputs_dir, input_files)

        return run_docker_validation(
            flow_id=flow_id,
            workspace=workspace,
            inputs_dir=inputs_dir,
            outputs_dir=outputs_dir,
            input_manifest_path=manifest_path,
            output_manifest_path=outputs_dir / "output_manifest.json",
            context_path=context_path,
            expected_outputs=artifact.data_contract.outputs,
            timeout_seconds=timeout_seconds,
            checks=checks,
            parameters=parameters,
            run_id=run_id,
            stage_callback=stage_callback,
        )


def execute_node_with_docker_handoff(
    *,
    flow_id: str,
    artifact: GeneratedArtifact,
    input_files: list[FileDescriptor],
    handoff_dir: Path,
    timeout_seconds: int,
    run_id: str | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> SandboxNodeRunResult:
    checks = [
        "docker_validation_enabled",
        "docker_build",
        "sample_input_manifest",
        "container_sample_run",
        "output_manifest_contract",
        "output_file_shape",
        "edge_output_handoff",
    ]
    if not docker_validation_enabled():
        report = ValidationReport(
            status="not_run",
            checks=checks,
            warnings=[
                f"Docker execution validation skipped; set {DOCKER_VALIDATION_ENV}=true."
            ],
        )
        return SandboxNodeRunResult(
            flow_id=flow_id,
            validation_report=report,
            inputs=input_files,
            outputs=artifact.data_contract.outputs,
        )

    with validation_workspace(flow_id) as tmp:
        workspace = Path(tmp)
        inputs_dir = workspace / "inputs"
        outputs_dir = workspace / "outputs"
        context_path = workspace / "context.json"
        inputs_dir.mkdir()
        outputs_dir.mkdir()
        context_path.write_text("{}\n", encoding="utf-8")
        write_generated_files(workspace, artifact)
        manifest_path = inputs_dir / "input_manifest.json"
        write_sample_inputs(manifest_path, inputs_dir, input_files)

        report = run_docker_validation(
            flow_id=flow_id,
            workspace=workspace,
            inputs_dir=inputs_dir,
            outputs_dir=outputs_dir,
            input_manifest_path=manifest_path,
            output_manifest_path=outputs_dir / "output_manifest.json",
            context_path=context_path,
            expected_outputs=artifact.data_contract.outputs,
            timeout_seconds=timeout_seconds,
            checks=checks,
            run_id=run_id,
            stage_callback=stage_callback,
        )
        outputs: list[FileDescriptor] = []
        if report.status == "valid":
            outputs = persist_descriptors_for_handoff(
                output_descriptors_from_manifest(
                    outputs_dir / "output_manifest.json",
                    outputs_dir,
                ),
                handoff_dir,
            )
        return SandboxNodeRunResult(
            flow_id=flow_id,
            validation_report=report,
            inputs=input_files,
            outputs=outputs,
        )


def validate_pipeline_with_docker(
    *,
    ordered_flow_ids: list[str],
    artifacts_by_node: dict[str, GeneratedArtifact],
    root_inputs_by_node: dict[str, list[FileDescriptor]],
    timeout_seconds: int,
) -> ValidationReport:
    checks = [
        "docker_validation_enabled",
        "docker_build",
        "sample_input_manifest",
        "container_sample_run",
        "output_manifest_contract",
        "output_file_shape",
        "edge_output_handoff",
    ]
    if not docker_validation_enabled():
        return ValidationReport(
            status="not_run",
            checks=checks,
            warnings=[
                f"Docker execution validation skipped; set {DOCKER_VALIDATION_ENV}=true."
            ],
        )

    errors: list[str] = []
    warnings: list[str] = []
    produced_outputs: dict[str, list[FileDescriptor]] = {}

    with validation_workspace("pipeline-handoff") as handoff_tmp:
        handoff_root = Path(handoff_tmp)
        for flow_id in ordered_flow_ids:
            artifact = artifacts_by_node.get(flow_id)
            if artifact is None:
                continue
            with validation_workspace(flow_id) as tmp:
                workspace = Path(tmp)
                inputs_dir = workspace / "inputs"
                outputs_dir = workspace / "outputs"
                context_path = workspace / "context.json"
                inputs_dir.mkdir()
                outputs_dir.mkdir()
                context_path.write_text("{}\n", encoding="utf-8")
                write_generated_files(workspace, artifact)

                inherited_inputs: list[FileDescriptor] = []
                for parent_id, outputs in produced_outputs.items():
                    for output in outputs:
                        source_path = existing_sample_file(output.sample)
                        if source_path is not None:
                            relative = Path(parent_id) / source_path.name
                            target_path = inputs_dir / relative
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source_path, target_path)
                            inherited_inputs.append(
                                FileDescriptor(
                                    filename=str(relative),
                                    kind=output.kind,
                                    format=output.format,
                                    columns=output.columns,
                                    required_columns=output.required_columns,
                                    schema=output.schema,
                                    semantic_role=output.semantic_role,
                                    sample=FileSample(text=str(target_path)),
                                )
                            )

                manifest_path = inputs_dir / "input_manifest.json"
                write_sample_inputs(
                    manifest_path,
                    inputs_dir,
                    [*root_inputs_by_node.get(flow_id, []), *inherited_inputs],
                )
                report = run_docker_validation(
                    flow_id=flow_id,
                    workspace=workspace,
                    inputs_dir=inputs_dir,
                    outputs_dir=outputs_dir,
                    input_manifest_path=manifest_path,
                    output_manifest_path=outputs_dir / "output_manifest.json",
                    context_path=context_path,
                    expected_outputs=artifact.data_contract.outputs,
                    timeout_seconds=timeout_seconds,
                    checks=checks,
                )
                errors.extend([f"Node {flow_id}: {error}" for error in report.errors])
                warnings.extend(
                    [f"Node {flow_id}: {warning}" for warning in report.warnings]
                )
                if report.status == "valid":
                    produced_outputs[flow_id] = persist_descriptors_for_handoff(
                        output_descriptors_from_manifest(
                            outputs_dir / "output_manifest.json",
                            outputs_dir,
                        ),
                        handoff_root / flow_id,
                    )

    return ValidationReport(
        status="invalid" if errors else "valid",
        checks=checks,
        errors=errors,
        warnings=warnings,
    )


def persist_descriptors_for_handoff(
    descriptors: list[FileDescriptor],
    handoff_dir: Path,
) -> list[FileDescriptor]:
    persisted: list[FileDescriptor] = []
    for descriptor in descriptors:
        source_path = existing_sample_file(descriptor.sample)
        if source_path is None:
            continue
        handoff_dir.mkdir(parents=True, exist_ok=True)
        target_path = handoff_dir / source_path.name
        shutil.copy2(source_path, target_path)
        persisted.append(
            FileDescriptor(
                filename=descriptor.filename,
                kind=descriptor.kind,
                format=descriptor.format,
                columns=descriptor.columns,
                required_columns=descriptor.required_columns,
                schema=descriptor.schema,
                semantic_role=descriptor.semantic_role,
                sample=FileSample(
                    rows=sample_rows_for_descriptor(target_path, descriptor),
                    text=str(target_path),
                ),
            )
        )
    return persisted


def sample_rows_for_descriptor(
    path: Path,
    descriptor: FileDescriptor,
    *,
    limit: int = 3,
) -> list[dict[str, object]]:
    if descriptor.kind == "table" and descriptor.format in {"csv", "tsv"}:
        delimiter = "\t" if descriptor.format == "tsv" else ","
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                return [
                    dict(row)
                    for _, row in zip(
                        range(limit), csv.DictReader(handle, delimiter=delimiter)
                    )
                ]
        except OSError:
            return []
    if descriptor.kind == "json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(payload, list):
            return [item for item in payload[:limit] if isinstance(item, dict)]
        if isinstance(payload, dict):
            return [payload]
    return []


def write_generated_files(workspace: Path, artifact: GeneratedArtifact) -> None:
    for file_item in artifact.files:
        if file_item.filename == "validation-report.json":
            continue
        path = workspace / file_item.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_item.content, encoding="utf-8")


def validation_workspace(flow_id: str):
    root = Path(os.getenv(VALIDATION_WORKDIR_ENV, DEFAULT_VALIDATION_WORKDIR))
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(
        prefix=f"inlumen-codegen-{flow_id}-",
        dir=str(root),
    )


def existing_sample_file(sample: FileSample | None) -> Path | None:
    """Return a real sample file without mistaking inline sample text for a path."""
    raw_path = str(sample.text or "").strip() if sample else ""
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path)
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        # JSON/text descriptors store literal content in ``sample.text``. Long or
        # multi-line values are valid samples but are not valid filesystem paths.
        return None


def write_sample_inputs(
    manifest_path: Path,
    inputs_dir: Path,
    input_files: list[FileDescriptor],
) -> None:
    entries = []
    for file_item in input_files:
        relative = Path(file_item.filename)
        path = inputs_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            sample_path = existing_sample_file(file_item.sample)
            if sample_path is not None:
                shutil.copy2(sample_path, path)
            elif write_embedded_media(path, file_item) or fetch_configured_input(path, file_item):
                pass
            else:
                write_sample_file(path, file_item)
        entries.append(
            {
                "name": Path(file_item.filename).stem,
                "filename": file_item.filename,
                "path": f"/inlumen/inputs/{file_item.filename}",
                "kind": file_item.kind or "binary",
                "format": file_item.format,
                "columns": file_item.columns,
                "required_columns": file_item.required_columns,
                "schema": file_item.schema,
                "semantic_role": file_item.semantic_role,
            }
        )
    manifest = {
        "schema_version": "inlumen.input-manifest@1",
        "inputs": entries,
        "files": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def write_embedded_media(path: Path, file_item: FileDescriptor) -> bool:
    data_uri = file_item.sample.data_uri if file_item.sample else None
    if not data_uri or not data_uri.startswith("data:") or "," not in data_uri:
        return False
    _, encoded = data_uri.split(",", 1)
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise RuntimeError(f"Invalid embedded sample for {file_item.filename}") from exc
    max_bytes = int(os.getenv(INPUT_FILE_MAX_BYTES_ENV) or 50 * 1024 * 1024)
    if len(content) > max_bytes:
        raise RuntimeError(
            f"Embedded sample {file_item.filename} exceeds {max_bytes} bytes"
        )
    path.write_bytes(content)
    return True


def fetch_configured_input(path: Path, file_item: FileDescriptor) -> bool:
    base_url = os.getenv(INPUT_FILE_BASE_URL_ENV, "").strip()
    if not base_url or not file_item.bucket:
        return False
    parsed = urlsplit(base_url)
    allow_http = os.getenv(INPUT_FILE_ALLOW_HTTP_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if (
        parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError(
            f"{INPUT_FILE_BASE_URL_ENV} must be a configured HTTPS URL "
            "without embedded credentials"
        )
    bucket_id = str(file_item.bucket).removeprefix("files-step-id-")
    max_bytes = int(os.getenv(INPUT_FILE_MAX_BYTES_ENV) or 50 * 1024 * 1024)
    headers = {}
    api_key = os.getenv("CODEGEN_INPUT_FILE_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.stream(
            "GET",
            base_url,
            params={
                "container_id": bucket_id,
                "filename": file_item.filename,
            },
            headers=headers,
            timeout=30,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("content-length") or 0)
            if content_length > max_bytes:
                raise RuntimeError(
                    f"Input {file_item.filename} exceeds {max_bytes} bytes"
                )
            written = 0
            with path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError(
                            f"Input {file_item.filename} exceeds {max_bytes} bytes"
                        )
                    handle.write(chunk)
    except (httpx.HTTPError, OSError) as exc:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Could not fetch real validation input {file_item.filename}: {exc}"
        ) from exc
    return True


def write_sample_file(path: Path, file_item: FileDescriptor) -> None:
    sample = file_item.sample
    rows = sample.rows if sample else []
    text = sample.text if sample else None
    if file_item.kind == "table" and file_item.format in {"csv", "tsv"}:
        delimiter = "\t" if file_item.format == "tsv" else ","
        columns = file_item.columns or sorted({key for row in rows for key in row})
        if not columns:
            columns = ["value"]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, delimiter=delimiter)
            writer.writeheader()
            writer.writerows(rows or [{column: "" for column in columns}])
        return
    if file_item.kind == "json":
        payload = rows if rows else json.loads(text) if text else []
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return
    if file_item.kind == "text":
        path.write_text(text or "", encoding="utf-8")
        return
    file_format = str(file_item.format or path.suffix.lstrip(".")).lower()
    if file_format in {"wav", "wave"}:
        sample_rate = 16000
        frames = [
            int(12000 * math.sin(2 * math.pi * 220 * index / sample_rate))
            for index in range(sample_rate)
        ]
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(struct.pack(f"<{len(frames)}h", *frames))
        return
    if file_format == "png":
        path.write_bytes(_validation_png_bytes())
        return
    if file_format == "pdf":
        path.write_bytes(_validation_pdf_bytes())
        return
    path.write_bytes((text or "").encode("utf-8"))


def _validation_png_bytes(width: int = 64, height: int = 64) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(
                (
                    (x * 255) // max(1, width - 1),
                    (y * 255) // max(1, height - 1),
                    ((x + y) * 255) // max(1, width + height - 2),
                )
            )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )


def _validation_pdf_bytes() -> bytes:
    text = "InLumen multimodal pipeline validation document."
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def validate_pipeline_program_with_docker(
    *,
    pipeline_source: str,
    plan: dict,
    requirements: list[str],
    input_files: list[FileDescriptor],
    base_image: str,
    timeout_seconds: int,
    network_allowed: bool,
    run_id: str | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> ValidationReport:
    """Execute one complete pipeline using a dependency image cached by hash."""
    checks = [
        "docker_validation_enabled",
        "pipeline_dependency_image_cache",
        "pipeline_sample_input_manifest",
        "whole_pipeline_sample_run",
        "per_node_output_manifest_contract",
        "per_node_output_file_shape",
    ]
    if not docker_validation_enabled():
        return ValidationReport(
            status="not_run",
            checks=checks,
            warnings=[
                f"Docker execution validation skipped; set {DOCKER_VALIDATION_ENV}=true."
            ],
        )

    errors: list[str] = []
    warnings: list[str] = []
    with validation_workspace("pipeline-first") as tmp:
        workspace = Path(tmp)
        inputs_dir = workspace / "inputs"
        outputs_dir = workspace / "outputs"
        context_path = workspace / "context.json"
        pipeline_path = workspace / "pipeline.py"
        inputs_dir.mkdir()
        outputs_dir.mkdir()
        context_path.write_text(
            json.dumps(
                {
                    "pipeline": plan.get("pipeline") or {},
                    "validation": "pipeline_sample",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        pipeline_path.write_text(pipeline_source, encoding="utf-8")
        input_manifest = inputs_dir / "input_manifest.json"
        write_sample_inputs(input_manifest, inputs_dir, input_files)

        client = None
        container = None
        try:
            client = docker.from_env()
            image = _cached_dependency_image(
                client,
                workspace=workspace,
                requirements=requirements,
                base_image=base_image,
                run_id=run_id,
                stage_callback=stage_callback,
            )
            if stage_callback is not None:
                stage_callback("sandbox_execution")
            _raise_if_sandbox_cancelled(run_id)
            run_options = {
                "command": ["python", "/inlumen/pipeline.py"],
                "detach": True,
                "remove": False,
                "network_disabled": not network_allowed,
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges"],
                "pids_limit": 128,
                "mem_limit": os.getenv("CODEGEN_VALIDATION_MEMORY_LIMIT", "1g"),
                "nano_cpus": int(
                    float(os.getenv("CODEGEN_VALIDATION_CPU_LIMIT", "1"))
                    * 1_000_000_000
                ),
                "tmpfs": {"/tmp": "rw,noexec,nosuid,size=128m"},
                "environment": {
                    "INLUMEN_FLOW_ID": "pipeline",
                    "INLUMEN_INPUT_MANIFEST": ("/inlumen/inputs/input_manifest.json"),
                    "INLUMEN_OUTPUT_DIR": "/inlumen/outputs",
                    "INLUMEN_OUTPUT_MANIFEST": (
                        "/inlumen/outputs/output_manifest.json"
                    ),
                    "INLUMEN_CONTEXT_PATH": "/inlumen/context.json",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                "volumes": {
                    str(pipeline_path): {
                        "bind": "/inlumen/pipeline.py",
                        "mode": "ro",
                    },
                    str(inputs_dir): {
                        "bind": "/inlumen/inputs",
                        "mode": "ro",
                    },
                    str(outputs_dir): {
                        "bind": "/inlumen/outputs",
                        "mode": "rw",
                    },
                    str(context_path): {
                        "bind": "/inlumen/context.json",
                        "mode": "ro",
                    },
                },
                "stdout": True,
                "stderr": True,
                "labels": {SANDBOX_RUN_LABEL: run_id} if run_id else {},
            }
            container = client.containers.run(image.id, **run_options)
            _register_sandbox_container(run_id, container)
            try:
                result = container.wait(timeout=timeout_seconds)
            except ReadTimeout:
                container.kill()
                return ValidationReport(
                    status="invalid",
                    checks=checks,
                    errors=[
                        f"Whole-pipeline sample run timed out after {timeout_seconds}s."
                    ],
                )
            logs = container.logs(stdout=True, stderr=True)
            status_code = int(result.get("StatusCode", 1))
            if status_code != 0:
                return ValidationReport(
                    status="invalid",
                    checks=checks,
                    errors=[
                        (
                            f"Whole-pipeline sample run failed with exit code {status_code}: "
                            f"{logs.decode('utf-8', errors='replace')[:4000]}"
                        )
                    ],
                )
            if logs:
                warnings.append(
                    f"Pipeline output: {logs.decode('utf-8', errors='replace')[:1000]}"
                )
        except DockerException as exc:
            return ValidationReport(
                status="invalid",
                checks=checks,
                errors=[f"Docker pipeline validation failed: {exc}"],
            )
        finally:
            _unregister_sandbox_container(run_id, container)
            if container is not None:
                try:
                    container.remove(force=True)
                except DockerException:
                    # Cleanup is best effort after the validation result is known.
                    pass
            if client is not None:
                try:
                    client.close()
                except DockerException:
                    # Cleanup is best effort after the validation result is known.
                    pass

        for node in plan.get("nodes") or []:
            flow_id = str(node.get("flow_id") or "")
            node_dir = outputs_dir / "nodes" / flow_id
            expected = [
                ExpectedArtifact.model_validate(item)
                for item in node.get("outputs") or []
            ]
            node_errors = validate_output_manifest(
                output_manifest_path=node_dir / "output_manifest.json",
                outputs_dir=node_dir,
                expected_outputs=expected,
            )
            errors.extend(f"Node {flow_id}: {error}" for error in node_errors)

    return ValidationReport(
        status="invalid" if errors else "valid",
        checks=checks,
        errors=errors,
        warnings=warnings,
    )


def validate_pipeline_dependencies_with_docker(
    *,
    requirements: list[str],
    base_image: str,
) -> ValidationReport:
    """Validate reviewed declarations without materializing a multi-gigabyte image."""
    checks = [
        "docker_validation_enabled",
        "docker_runtime_available",
        "reviewed_model_dependency_declarations",
        "model_runtime_execution_deferred",
    ]
    if not docker_validation_enabled():
        return ValidationReport(
            status="not_run",
            checks=checks,
            warnings=[
                f"Docker dependency validation skipped; set {DOCKER_VALIDATION_ENV}=true."
            ],
        )
    errors: list[str] = []
    seen: set[str] = set()
    for raw in requirements:
        try:
            parsed = Requirement(raw)
        except InvalidRequirement as exc:
            errors.append(f"Invalid reviewed dependency {raw!r}: {exc}")
            continue
        normalized = parsed.name.lower().replace("_", "-")
        if parsed.url:
            errors.append(
                f"Reviewed dependency {parsed.name} must not use a direct URL."
            )
        if normalized in seen:
            errors.append(f"Duplicate reviewed dependency: {parsed.name}")
        seen.add(normalized)
    client = None
    try:
        client = docker.from_env()
        client.ping()
        try:
            client.images.get(base_image)
        except ImageNotFound:
            client.images.pull(base_image)
    except DockerException as exc:
        errors.append(f"Docker runtime validation failed: {exc}")
    finally:
        if client is not None:
            try:
                client.close()
            except DockerException:
                # Cleanup is best effort after the validation result is known.
                pass
    return ValidationReport(
        status="invalid" if errors else "valid",
        checks=checks,
        errors=errors,
        warnings=[
            (
                "External model download and inference are deferred during codegen "
                "validation. The service validated dependency declarations, model "
                "identity, source semantics, contracts, security, and compilation; "
                "package installation and real model inference remain deployment "
                "preflight/runtime validations."
            )
        ],
    )


def _cached_dependency_image(
    client,
    *,
    workspace: Path,
    requirements: list[str],
    base_image: str,
    run_id: str | None = None,
    stage_callback: Callable[[str], None] | None = None,
):
    _raise_if_sandbox_cancelled(run_id)
    if not requirements:
        try:
            return client.images.get(base_image)
        except ImageNotFound:
            return client.images.pull(base_image)

    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "base_image": base_image,
                "requirements": sorted(requirements),
                "python": "3.11",
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    image_tag = f"inlumen-codegen-validation:deps-{fingerprint}"
    try:
        return client.images.get(image_tag)
    except ImageNotFound:
        pass

    requirements_text = "\n".join(requirements)
    if requirements_text:
        requirements_text += "\n"
    if stage_callback is not None:
        stage_callback("dependency_installation")

    install_timeout = max(
        1,
        int(
            os.getenv(DEPENDENCY_INSTALL_TIMEOUT_ENV)
            or DEFAULT_DEPENDENCY_INSTALL_TIMEOUT_SECONDS
        ),
    )
    container = None
    try:
        try:
            client.images.get(base_image)
        except ImageNotFound:
            client.images.pull(base_image)
        labels = {SANDBOX_RUN_LABEL: run_id} if run_id else {}
        container = client.containers.create(
            base_image,
            command=[
                "python",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "-r",
                "/tmp/inlumen-requirements.txt",
            ],
            labels=labels,
            mem_limit=os.getenv("CODEGEN_VALIDATION_MEMORY_LIMIT", "1g"),
            nano_cpus=int(
                float(os.getenv("CODEGEN_VALIDATION_CPU_LIMIT", "1")) * 1_000_000_000
            ),
            pids_limit=256,
            security_opt=["no-new-privileges"],
        )
        _register_sandbox_container(run_id, container)
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
            content = requirements_text.encode("utf-8")
            info = tarfile.TarInfo(name="inlumen-requirements.txt")
            info.size = len(content)
            info.mode = 0o444
            archive.addfile(info, io.BytesIO(content))
        archive_buffer.seek(0)
        if not container.put_archive("/tmp", archive_buffer.read()):
            raise SandboxUnavailable(
                "Could not copy requirements into dependency installer."
            )
        _raise_if_sandbox_cancelled(run_id)
        container.start()
        try:
            result = container.wait(timeout=install_timeout)
        except ReadTimeout as exc:
            container.kill()
            raise SandboxUnavailable(
                "Dependency installation exceeded the "
                f"{install_timeout}s wall-clock deadline."
            ) from exc
        _raise_if_sandbox_cancelled(run_id)
        status_code = int(result.get("StatusCode", 1))
        if status_code != 0:
            try:
                logs = container.logs(stdout=True, stderr=True)
            except DockerException:
                logs = b""
            raise SandboxUnavailable(
                "Dependency installation failed with exit code "
                f"{status_code}: {logs.decode('utf-8', errors='replace')[:4000]}"
            )
        repository, tag = image_tag.split(":", 1)
        return container.commit(repository=repository, tag=tag)
    finally:
        _unregister_sandbox_container(run_id, container)
        if container is not None:
            try:
                container.remove(force=True)
            except DockerException:
                pass


def run_docker_validation(
    *,
    flow_id: str,
    workspace: Path,
    inputs_dir: Path,
    outputs_dir: Path,
    input_manifest_path: Path,
    output_manifest_path: Path,
    context_path: Path,
    expected_outputs: list[ExpectedArtifact],
    timeout_seconds: int,
    checks: list[str],
    parameters: dict[str, object] | None = None,
    run_id: str | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    base_image = "python:3.11-slim"
    manifest_path = workspace / "node-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = {}
        runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
        if isinstance(runtime, dict) and str(runtime.get("base_image") or "").strip():
            base_image = str(runtime["base_image"]).strip()
    requirements_path = workspace / "requirements.txt"
    requirements = (
        [
            line.strip()
            for line in requirements_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if requirements_path.is_file()
        else []
    )
    if "dependency_image_cache" not in checks:
        checks.append("dependency_image_cache")
    client = None
    container = None
    try:
        client = docker.from_env()
        image = _cached_dependency_image(
            client,
            workspace=workspace,
            requirements=requirements,
            base_image=base_image,
            run_id=run_id,
            stage_callback=stage_callback,
        )
        if stage_callback is not None:
            stage_callback("sandbox_execution")
        _raise_if_sandbox_cancelled(run_id)
        runtime_parameters = {
            str(key): value
            for key, value in (parameters or {}).items()
            if str(key).strip() and str(key) != "model_plan"
        }
        runtime_environment = {
            "PIPELINE_INPUT_DIR": "/inlumen/inputs",
            "PIPELINE_OUTPUT_DIR": "/inlumen/outputs",
            "INLUMEN_FLOW_ID": flow_id,
            "INLUMEN_INPUT_MANIFEST": "/inlumen/inputs/input_manifest.json",
            "INLUMEN_OUTPUT_DIR": "/inlumen/outputs",
            "INLUMEN_OUTPUT_MANIFEST": "/inlumen/outputs/output_manifest.json",
            "INLUMEN_CONTEXT_PATH": "/inlumen/context.json",
        }
        if runtime_parameters:
            runtime_environment["PIPELINE_PARAMS_JSON"] = json.dumps(
                runtime_parameters,
                ensure_ascii=False,
                sort_keys=True,
            )
            runtime_environment["INLUMEN_PARAMS_JSON"] = json.dumps(
                runtime_parameters,
                ensure_ascii=False,
                sort_keys=True,
            )
        for key, value in sorted(runtime_parameters.items()):
            env_name = "INLUMEN_PARAM_" + re.sub(
                r"[^A-Za-z0-9]+", "_", key
            ).upper().strip("_")
            if env_name != "INLUMEN_PARAM_":
                runtime_environment[env_name] = str(value)
                runtime_environment[env_name.replace("INLUMEN_", "PIPELINE_")] = str(value)
            if (
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                and not key.startswith(("PIPELINE_", "INLUMEN_"))
            ):
                runtime_environment[key] = str(value)
        container = client.containers.run(
            image.id,
            command=["python", "/app/main.py"],
            detach=True,
            remove=False,
            network_disabled=True,
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            environment=runtime_environment,
            volumes={
                str(workspace): {"bind": "/app", "mode": "ro"},
                str(inputs_dir): {"bind": "/inlumen/inputs", "mode": "ro"},
                str(outputs_dir): {"bind": "/inlumen/outputs", "mode": "rw"},
                str(context_path): {"bind": "/inlumen/context.json", "mode": "ro"},
            },
            stdout=True,
            stderr=True,
            labels={SANDBOX_RUN_LABEL: run_id} if run_id else {},
        )
        _register_sandbox_container(run_id, container)
        try:
            result = container.wait(timeout=timeout_seconds)
        except ReadTimeout:
            container.kill()
            return ValidationReport(
                status="invalid",
                checks=checks,
                errors=[f"Container sample run timed out after {timeout_seconds}s."],
            )
        logs = container.logs(stdout=True, stderr=True)
        status_code = int(result.get("StatusCode", 1))
        if status_code != 0:
            return ValidationReport(
                status="invalid",
                checks=checks,
                errors=[
                    (
                        f"Container sample run failed with exit code {status_code}: "
                        f"{logs.decode('utf-8', errors='replace')[:2000]}"
                    )
                ],
            )
        if logs:
            warnings.append(
                f"Container output: {logs.decode('utf-8', errors='replace')[:1000]}"
            )
    except (DockerException, SandboxUnavailable) as exc:
        return ValidationReport(
            status="invalid",
            checks=checks,
            errors=[f"Docker validation failed: {exc}"],
        )
    finally:
        _unregister_sandbox_container(run_id, container)
        if container is not None:
            try:
                container.remove(force=True)
            except DockerException:
                # Cleanup is best effort after the validation result is known.
                pass
        if client is not None:
            try:
                client.close()
            except DockerException:
                pass

    errors.extend(
        validate_output_manifest(
            output_manifest_path=output_manifest_path,
            outputs_dir=outputs_dir,
            expected_outputs=expected_outputs,
        )
    )
    return ValidationReport(
        status="invalid" if errors else "valid",
        checks=checks,
        errors=errors,
        warnings=warnings,
    )


def validate_output_manifest(
    *,
    output_manifest_path: Path,
    outputs_dir: Path,
    expected_outputs: list[ExpectedArtifact],
) -> list[str]:
    if not output_manifest_path.exists():
        # The filesystem runtime discovers outputs after the process exits. A
        # manifest is accepted only as a compatibility path for older code.
        outputs = [
            {
                "name": path.stem,
                "filename": path.relative_to(outputs_dir).as_posix(),
                "path": str(path),
            }
            for path in sorted(outputs_dir.rglob("*"))
            if path.is_file()
        ]
        if not outputs:
            return ["Generated script did not produce files in PIPELINE_OUTPUT_DIR."]
    else:
        try:
            manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return [f"Output manifest is invalid JSON: {exc}"]
        outputs = manifest.get("outputs") or manifest.get("files") or []
        if not isinstance(outputs, list):
            return ["Output manifest must contain an outputs list."]
    by_name = {
        str(item.get("name") or Path(str(item.get("filename") or "")).stem): item
        for item in outputs
        if isinstance(item, dict)
    }
    errors: list[str] = []
    for expected in expected_outputs:
        actual = by_name.get(expected.name)
        if actual is None:
            actual = next(
                (
                    item for item in outputs
                    if isinstance(item, dict)
                    and Path(str(item.get("filename") or "")).name == expected.filename
                ),
                None,
            )
        if actual is None:
            errors.append(
                f"Missing declared output in output manifest: {expected.name}"
            )
            continue
        if actual.get("kind") and actual.get("kind") != expected.kind:
            errors.append(
                f"Output {expected.name} kind mismatch: expected {expected.kind}, "
                f"got {actual.get('kind')}"
            )
        if (
            expected.format
            and actual.get("format")
            and actual.get("format") != expected.format
        ):
            errors.append(
                f"Output {expected.name} format mismatch: expected {expected.format}, "
                f"got {actual.get('format')}"
            )
        output_path = output_path_from_manifest_item(actual, outputs_dir)
        if output_path is None or not output_path.exists():
            errors.append(f"Output file does not exist for {expected.name}.")
            continue
        errors.extend(validate_output_shape(output_path, expected))
    return errors


def output_path_from_manifest_item(item: dict, outputs_dir: Path) -> Path | None:
    raw_path = item.get("path") or item.get("filename")
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if path.is_absolute():
        return outputs_dir / path.name
    return outputs_dir / path


def validate_output_shape(path: Path, expected: ExpectedArtifact) -> list[str]:
    if expected.kind == "table" and expected.format in {"csv", "tsv"}:
        delimiter = "\t" if expected.format == "tsv" else ","
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            rows = list(reader)
            if not reader.fieldnames:
                return [f"Table output {expected.name} has no header columns."]
            if not rows:
                return [f"Table output {expected.name} has no sample rows."]
            missing_required = [
                column
                for column in expected.required_columns
                if column not in reader.fieldnames
            ]
            if missing_required:
                return [
                    f"Table output {expected.name} is missing required columns: "
                    + ", ".join(missing_required)
                    + f". Found columns: {', '.join(reader.fieldnames)}"
                ]
    elif expected.kind == "json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return [f"JSON output {expected.name} is invalid: {exc}"]
        schema_errors = validate_json_payload_shape(payload, expected)
        if schema_errors:
            return schema_errors
    elif expected.kind == "model" and path.stat().st_size == 0:
        return [f"Model output {expected.name} is empty."]
    elif expected.kind in {"image", "audio", "video", "document", "binary"}:
        if path.stat().st_size == 0:
            return [f"Binary output {expected.name} is empty."]
        header = path.read_bytes()[:16]
        if expected.format == "png" and not header.startswith(b"\x89PNG\r\n\x1a\n"):
            return [f"Image output {expected.name} is not a valid PNG file."]
        if expected.format in {"wav", "wave"}:
            try:
                with wave.open(str(path), "rb") as handle:
                    if handle.getnframes() <= 0:
                        return [f"Audio output {expected.name} has no frames."]
            except (wave.Error, EOFError):
                return [f"Audio output {expected.name} is not a valid WAV file."]
        if expected.format == "pdf" and not header.startswith(b"%PDF-"):
            return [f"Document output {expected.name} is not a valid PDF file."]
    return []


def validate_json_payload_shape(
    payload: object, expected: ExpectedArtifact
) -> list[str]:
    schema = expected.schema or {}
    errors: list[str] = []
    schema_type = schema.get("type")
    if schema_type == "object" and not isinstance(payload, dict):
        return [f"JSON output {expected.name} must be an object."]
    if schema_type == "array" and not isinstance(payload, list):
        return [f"JSON output {expected.name} must be an array."]
    if isinstance(payload, dict):
        missing = [
            str(key)
            for key in schema.get("required", [])
            if isinstance(key, str) and key not in payload
        ]
        if missing:
            errors.append(
                f"JSON output {expected.name} is missing required keys: "
                + ", ".join(missing)
            )
        properties = (
            schema.get("properties")
            if isinstance(schema.get("properties"), dict)
            else {}
        )
        for key, property_schema in properties.items():
            if key not in payload or not isinstance(property_schema, dict):
                continue
            property_errors = validate_json_property_shape(
                output_name=expected.name,
                key=str(key),
                value=payload[key],
                schema=property_schema,
            )
            errors.extend(property_errors)
        if expected.semantic_role == "model_metrics":
            if not isinstance(payload.get("metrics"), dict):
                errors.append(f"JSON output {expected.name} metrics must be an object.")
            if not str(payload.get("target_column") or "").strip():
                errors.append(f"JSON output {expected.name} target_column must be set.")
        if expected.semantic_role == "alerts" and not isinstance(
            payload.get("alerts"), list
        ):
            errors.append(f"JSON output {expected.name} alerts must be an array.")
    return errors


def validate_json_property_shape(
    *,
    output_name: str,
    key: str,
    value: object,
    schema: dict,
) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type and not json_type_matches(value, str(expected_type)):
        errors.append(
            f"JSON output {output_name} key {key} must be "
            f"{expected_type}, got {type(value).__name__}."
        )
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        errors.append(
            f"JSON output {output_name} key {key} must be one of: "
            + ", ".join(str(item) for item in enum)
        )
    if isinstance(value, dict):
        required = {
            str(item)
            for item in schema.get("required", [])
            if isinstance(item, str)
        }
        missing = sorted(required - value.keys())
        if missing:
            errors.append(
                f"JSON output {output_name} key {key} is missing required keys: "
                + ", ".join(missing)
            )
        properties = (
            schema.get("properties")
            if isinstance(schema.get("properties"), dict)
            else {}
        )
        for child_key, child_schema in properties.items():
            if child_key not in value or not isinstance(child_schema, dict):
                continue
            errors.extend(
                validate_json_property_shape(
                    output_name=output_name,
                    key=f"{key}.{child_key}",
                    value=value[child_key],
                    schema=child_schema,
                )
            )
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        for index, item in enumerate(value):
            errors.extend(
                validate_json_property_shape(
                    output_name=output_name,
                    key=f"{key}[{index}]",
                    value=item,
                    schema=item_schema,
                )
            )
    return errors


def json_type_matches(value: object, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def output_descriptors_from_manifest(
    output_manifest_path: Path,
    outputs_dir: Path,
) -> list[FileDescriptor]:
    manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
    outputs = manifest.get("outputs") or manifest.get("files") or []
    descriptors = []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path") or item.get("filename")
        if not raw_path:
            continue
        path = Path(str(raw_path))
        host_path = (
            outputs_dir / path.name if path.is_absolute() else outputs_dir / path
        )
        columns = item.get("columns") if isinstance(item.get("columns"), list) else []
        if not columns and (
            item.get("kind") == "table" and item.get("format") in {"csv", "tsv"}
        ):
            columns = table_columns(host_path, str(item.get("format") or "csv"))
        descriptors.append(
            FileDescriptor(
                filename=path.name,
                kind=item.get("kind") or "binary",
                format=item.get("format"),
                columns=columns,
                required_columns=(
                    item.get("required_columns")
                    if isinstance(item.get("required_columns"), list)
                    else []
                ),
                schema=item.get("schema")
                if isinstance(item.get("schema"), dict)
                else {},
                semantic_role=str(item.get("semantic_role") or ""),
                sample=FileSample(text=str(host_path)),
            )
        )
    return descriptors


def table_columns(path: Path, file_format: str) -> list[str]:
    if not path.exists():
        return []
    delimiter = "\t" if file_format == "tsv" else ","
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            return [str(column) for column in next(reader, [])]
    except OSError:
        return []


def safe_image_tag_fragment(value: str) -> str:
    text = "".join(char if char.isalnum() or char in "_.-" else "-" for char in value)
    return text.strip(".-") or "node"
