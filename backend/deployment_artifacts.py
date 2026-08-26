import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from artifact_content import decode_artifact_content, verify_artifact_integrity
from artifact_contract import ArtifactBinding, artifact_bindings, classify_artifact
from filesystem_runtime import filesystem_shell_component_source
from node_parameters import normalize_secret_param_keys
from node_ports import normalize_node_ports
from node_secrets import runtime_secret_name
from runtime_environment import discover_runtime_environment, merge_runtime_environment
from step_types import normalize_step_type

try:
    import yaml
except Exception:  # pragma: no cover - runtime image installs PyYAML.
    yaml = None


DOCKERFILE_INSTRUCTIONS = {
    "ADD",
    "ARG",
    "CMD",
    "COPY",
    "ENTRYPOINT",
    "ENV",
    "EXPOSE",
    "FROM",
    "HEALTHCHECK",
    "LABEL",
    "ONBUILD",
    "RUN",
    "SHELL",
    "STOPSIGNAL",
    "USER",
    "VOLUME",
    "WORKDIR",
}
DOCKERFILE_NAME_RE = re.compile(r"^Dockerfile\.([A-Za-z0-9][A-Za-z0-9_.-]*)$")
STEP_ID_RE = re.compile(r"files-step-id-([^/]+)$")
CODEGEN_GENERATOR = "inlumen-codegen-service"
ATTACHED_RUNTIME_GENERATOR = "inlumen-attached-runtime"
MANAGED_ADAPTER_GENERATOR = "inlumen-managed-adapter"
DAGSTER_PINNED_VERSION = "1.13.12"
DAGSTER_LIBRARY_PINNED_VERSION = "0.29.12"
UV_PINNED_VERSION = "0.11.32"
ARTIFACT_CONTRACT = {
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
_DIRECT_PARAMETER_ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_PARAMETER_ENVIRONMENT_NAMES = {
    "PIPELINE_INPUT_DIR",
    "PIPELINE_OUTPUT_DIR",
    "PIPELINE_PARAMS_JSON",
}

_ARGO_PORT_RUNNER = r'''import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

def same_file_contents(first, second):
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as first_handle, second.open("rb") as second_handle:
        while True:
            first_chunk = first_handle.read(1024 * 1024)
            second_chunk = second_handle.read(1024 * 1024)
            if first_chunk != second_chunk:
                return False
            if not first_chunk:
                return True


def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def publish_staged_directory(staging, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if destination.exists() or destination.is_symlink():
        previous = Path(tempfile.mkdtemp(
            prefix=f".{destination.name}.previous-",
            dir=destination.parent,
        ))
        previous.rmdir()
        os.replace(destination, previous)
    try:
        os.replace(staging, destination)
    except Exception:
        if previous is not None and (previous.exists() or previous.is_symlink()):
            os.replace(previous, destination)
        raise
    else:
        if previous is not None:
            remove_path(previous)


command = json.loads(sys.argv[1])
ports = [str(value).strip() for value in json.loads(sys.argv[2]) if str(value).strip()]
if len(ports) > 1:
    raise RuntimeError(
        "The flat Task workspace supports exactly one logical output set; "
        "fan out one output to multiple consumers or add an explicit split Task."
    )
requirements = json.loads(sys.argv[3])
staging_roots = [Path(value) for value in json.loads(sys.argv[4])]
missing = [
    item["name"] for item in requirements
    if item.get("required") and not os.getenv(str(item.get("name") or ""))
]
if missing:
    raise RuntimeError(
        "Missing required runtime environment variable(s): " + ", ".join(sorted(missing))
    )
if staging_roots:
    input_dir = Path(os.environ["PIPELINE_INPUT_DIR"])
    input_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(
        prefix=f".{input_dir.name}.staging-",
        dir=input_dir.parent,
    ))
    owners = {}
    try:
        for source_root in staging_roots:
            if not source_root.is_dir():
                raise RuntimeError(f"Required upstream artifact is missing: {source_root}")
            for source in sorted(source_root.rglob("*")):
                if not source.is_file():
                    continue
                relative = source.relative_to(source_root)
                destination = staging_dir / relative
                existing = owners.get(relative)
                if existing is not None:
                    if same_file_contents(source, existing):
                        continue
                    raise RuntimeError(
                        f"Upstream artifacts collide at {relative.as_posix()!r}; "
                        "add a Task that merges or renames them."
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                owners[relative] = source
        publish_staged_directory(staging_dir, input_dir)
    except Exception:
        remove_path(staging_dir)
        raise
result = subprocess.run(command)
if result.returncode:
    raise SystemExit(result.returncode)
output_dir = Path(os.environ["PIPELINE_OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
declared = set(ports)
root_entries = [
    path for path in sorted(output_dir.iterdir())
    if path.name not in declared and path.name != ".gitkeep"
]
if root_entries:
    if len(ports) != 1:
        names = ", ".join(path.name for path in root_entries)
        raise RuntimeError(
            "Task wrote artifacts outside declared output-port directories: " + names
        )
    port_dir = output_dir / ports[0]
    port_dir.mkdir(parents=True, exist_ok=True)
    for source in root_entries:
        destination = port_dir / source.name
        if destination.exists():
            raise RuntimeError(f"Output collision at {destination}")
        shutil.move(str(source), str(destination))
for port in ports:
    (output_dir / port).mkdir(parents=True, exist_ok=True)
'''


class DeploymentArtifactValidationError(ValueError):
    """Raised when generated deployment artifacts fail the format guardrails."""

    def __init__(self, message: str, errors: Sequence[str]):
        self.errors = list(errors)
        detail = "; ".join(self.errors)
        super().__init__(f"{message}: {detail}" if detail else message)


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _step_secret_parameters(step: dict[str, Any]) -> list[str]:
    return normalize_secret_param_keys(
        step.get("secret_params"),
        step.get("param") if isinstance(step.get("param"), dict) else {},
    )


def _step_runtime_parameters(step: dict[str, Any]) -> dict[str, Any]:
    secret_names = set(_step_secret_parameters(step))
    return {
        str(key): value
        for key, value in (step.get("param") or {}).items()
        if str(key).strip()
        and str(key) != "model_plan"
        and str(key) not in secret_names
    }


def _direct_parameter_environment_name(key: Any) -> str:
    """Return a safe direct environment variable name for a Task parameter."""
    name = str(key).strip()
    if (
        not _DIRECT_PARAMETER_ENVIRONMENT_RE.fullmatch(name)
        or name in _RESERVED_PARAMETER_ENVIRONMENT_NAMES
        or name.startswith(("PIPELINE_", "INLUMEN_"))
    ):
        return ""
    return name


def _capability_secret_names(manifest: dict) -> list[str]:
    capabilities = manifest.get("capabilities")
    values = capabilities.get("secrets") if isinstance(capabilities, dict) else []
    if not isinstance(values, list):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _runtime_secret_parameters(
    step: dict[str, Any],
    dockerfiles_payload: Any,
) -> list[str]:
    """Combine graph-configured and package-declared secret references."""
    flow_id = _clean_string(step.get("flow_id"))
    manifest = _json_object(
        _deployment_file_content(
            dockerfiles_payload,
            flow_id,
            "node-manifest.json",
        )
    )
    return sorted(set(_step_secret_parameters(step)) | set(_capability_secret_names(manifest)))


def _kubernetes_secret_key(step_id: Any, parameter_name: Any) -> str:
    node_fragment = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(step_id or "")).strip("-.")
    parameter_fragment = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", str(parameter_name or "")
    ).strip("-.")
    return f"{node_fragment}.{parameter_fragment}"


def _sanitize_fragment(value: Any, fallback: str) -> str:
    text = _clean_string(value).lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text).strip(".-")
    return text or fallback


def _argo_name(value: Any, prefix: str = "step") -> str:
    text = _clean_string(value).lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text).strip("-")
    if not text:
        text = prefix
    if not re.match(r"^[a-z]", text):
        text = f"{prefix}-{text}"
    return text[:63].rstrip("-")


def _json_array(items: Sequence[str]) -> str:
    return json.dumps(list(items))


def step_id_from_bucket(bucket: Any) -> str:
    match = STEP_ID_RE.search(_clean_string(bucket))
    return match.group(1) if match else ""


def normalize_file_refs(files: Any) -> List[dict]:
    if not files:
        return []
    if not isinstance(files, list):
        raise ValueError("files must be a list of {filename, bucket} objects.")

    normalized: List[dict] = []
    for idx, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"files[{idx}] must be an object.")

        filename = _clean_string(entry.get("filename"))
        bucket = _clean_string(entry.get("bucket"))
        step_id = _clean_string(entry.get("step_id") or entry.get("flow_id"))
        if not step_id:
            step_id = step_id_from_bucket(bucket)
        if not filename:
            raise ValueError(f"files[{idx}] is missing filename.")
        if not step_id:
            raise ValueError(
                f"Could not extract step id from bucket '{bucket}' for file '{filename}'."
            )
        normalized.append(
            {
                "filename": filename,
                "bucket": bucket,
                "step_id": step_id,
                **(
                    {"role": _clean_string(entry.get("role")).lower()}
                    if _clean_string(entry.get("role")).lower()
                    in {"code", "data"}
                    else {}
                ),
                **(
                    {"snapshot_bucket": _clean_string(entry.get("snapshot_bucket"))}
                    if _clean_string(entry.get("snapshot_bucket"))
                    else {}
                ),
                **(
                    {"snapshot_object": _clean_string(entry.get("snapshot_object"))}
                    if _clean_string(entry.get("snapshot_object"))
                    else {}
                ),
            }
        )
    return normalized


def _safe_docker_source(filename: str) -> str:
    path = PurePosixPath(filename)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"Unsafe Docker build context path '{filename}'.")
    return str(path)


def _files_from_step_data(data: dict, flow_id: str) -> List[dict]:
    file_refs: List[dict] = []
    for entry in data.get("file_buckets") or []:
        if not isinstance(entry, dict) or not entry.get("filename"):
            continue
        file_refs.append(
            {
                "filename": _clean_string(entry.get("filename")),
                "bucket": _clean_string(entry.get("bucket"))
                or f"files-step-id-{flow_id}",
                "step_id": flow_id,
                **(
                    {"role": _clean_string(entry.get("role")).lower()}
                    if _clean_string(entry.get("role")).lower()
                    in {"code", "data"}
                    else {}
                ),
                **(
                    {"snapshot_bucket": _clean_string(entry.get("snapshot_bucket"))}
                    if _clean_string(entry.get("snapshot_bucket"))
                    else {}
                ),
                **(
                    {"snapshot_object": _clean_string(entry.get("snapshot_object"))}
                    if _clean_string(entry.get("snapshot_object"))
                    else {}
                ),
            }
        )

    if file_refs:
        return file_refs

    for entry in data.get("files") or []:
        if isinstance(entry, str):
            filename = entry.strip()
            metadata = {}
        elif isinstance(entry, dict):
            filename = _clean_string(entry.get("filename") or entry.get("name"))
            metadata = entry
        else:
            continue
        if not filename:
            continue
        role = _clean_string(metadata.get("role")).lower()
        file_refs.append(
            {
                "filename": filename,
                "bucket": _clean_string(metadata.get("bucket"))
                or f"files-step-id-{flow_id}",
                "step_id": flow_id,
                **({"role": role} if role in {"code", "data"} else {}),
                **(
                    {"snapshot_bucket": _clean_string(metadata.get("snapshot_bucket"))}
                    if _clean_string(metadata.get("snapshot_bucket"))
                    else {}
                ),
                **(
                    {"snapshot_object": _clean_string(metadata.get("snapshot_object"))}
                    if _clean_string(metadata.get("snapshot_object"))
                    else {}
                ),
            }
        )
    return file_refs


def extract_pipeline_steps(pipeline_graph: Optional[dict], files: Any = None) -> List[dict]:
    """Return normalized steps with file refs from either Neo4j graph export shape."""
    normalized_files = normalize_file_refs(files)
    files_by_step: Dict[str, List[dict]] = defaultdict(list)
    for entry in normalized_files:
        files_by_step[entry["step_id"]].append(entry)

    steps_by_id: Dict[str, dict] = {}
    graph = pipeline_graph if isinstance(pipeline_graph, dict) else {}

    for row in graph.get("step_rows") or []:
        if not isinstance(row, dict):
            continue
        step_data = row.get("step") or {}
        flow_id = _clean_string(step_data.get("flow_id"))
        if not flow_id:
            continue
        step_type = normalize_step_type(step_data.get("type"))
        files_for_step = row.get("files") or []
        steps_by_id[flow_id] = {
            "flow_id": flow_id,
            "label": _clean_string(step_data.get("label")),
            "description": _clean_string(step_data.get("description")),
            "type": step_type,
            "template": _clean_string(step_data.get("template_label")),
            "ports": normalize_node_ports(
                step_data.get("ports") or step_data.get("ports_json"),
                step_type,
            ),
            "content": _clean_string(step_data.get("content")),
            "endpoint": _clean_string(step_data.get("endpoint")),
            "database": _clean_string(step_data.get("database")),
            "param": step_data.get("param") if isinstance(step_data.get("param"), dict) else {},
            "secret_params": normalize_secret_param_keys(
                step_data.get("secret_params") or step_data.get("secret_params_json"),
                step_data.get("param") if isinstance(step_data.get("param"), dict) else {},
            ),
            "definition_id": _clean_string(step_data.get("definition_id")),
            "definition_version": step_data.get("definition_version"),
            "implementation": (
                step_data.get("implementation")
                if isinstance(step_data.get("implementation"), dict)
                else _json_object(step_data.get("implementation_json"))
            ),
            "configuration_status": _clean_string(step_data.get("configuration_status")),
            "generated_artifact": (
                step_data.get("generated_artifact")
                if isinstance(step_data.get("generated_artifact"), dict)
                else _json_object(step_data.get("generated_artifact_json"))
            ),
            "files": normalize_file_refs(
                [
                    {
                        "filename": f.get("filename"),
                        "bucket": f.get("bucket") or f"files-step-id-{flow_id}",
                        "step_id": flow_id,
                        "snapshot_bucket": f.get("snapshot_bucket"),
                        "snapshot_object": f.get("snapshot_object"),
                        "role": f.get("role"),
                    }
                    for f in files_for_step
                    if isinstance(f, dict) and f.get("filename")
                ]
            ),
        }

    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else node
        flow_id = _clean_string(data.get("flow_id") or node.get("id") or data.get("id"))
        if not flow_id:
            continue
        step_type = normalize_step_type(data.get("type"))

        param = data.get("param") if isinstance(data.get("param"), dict) else {}
        if not param and isinstance(data.get("param_json"), str):
            try:
                parsed_param = json.loads(data.get("param_json") or "{}")
                param = parsed_param if isinstance(parsed_param, dict) else {}
            except Exception:
                param = {}

        steps_by_id[flow_id] = {
            "flow_id": flow_id,
            "label": _clean_string(data.get("label")),
            "description": _clean_string(data.get("description")),
            "type": step_type,
            "template": _clean_string(data.get("template_label")),
            "ports": normalize_node_ports(
                data.get("ports") or data.get("ports_json"),
                step_type,
            ),
            "content": _clean_string(data.get("content")),
            "endpoint": _clean_string(data.get("endpoint")),
            "database": _clean_string(data.get("database")),
            "param": param,
            "secret_params": normalize_secret_param_keys(
                data.get("secret_params") or data.get("secret_params_json"),
                param,
            ),
            "definition_id": _clean_string(data.get("definition_id")),
            "definition_version": data.get("definition_version"),
            "implementation": (
                data.get("implementation")
                if isinstance(data.get("implementation"), dict)
                else _json_object(data.get("implementation_json"))
            ),
            "configuration_status": _clean_string(data.get("configuration_status")),
            "generated_artifact": (
                data.get("generated_artifact")
                if isinstance(data.get("generated_artifact"), dict)
                else _json_object(data.get("generated_artifact_json"))
            ),
            "files": _files_from_step_data(data, flow_id),
        }

    for step_id, step_files in files_by_step.items():
        if step_id not in steps_by_id:
            steps_by_id[step_id] = {
                "flow_id": step_id,
                "label": "",
                "description": "",
                "type": "task",
                "template": "Blank Task",
                "ports": normalize_node_ports(None, "task"),
                "content": "",
                "endpoint": "",
                "database": "",
                "param": {},
                "secret_params": [],
                "definition_id": "",
                "definition_version": None,
                "implementation": {},
                "configuration_status": "",
                "generated_artifact": {},
                "files": [],
            }
        known = {(f["filename"], f.get("bucket", "")) for f in steps_by_id[step_id]["files"]}
        for file_ref in step_files:
            key = (file_ref["filename"], file_ref.get("bucket", ""))
            if key not in known:
                steps_by_id[step_id]["files"].append(file_ref)

    steps = list(steps_by_id.values())
    steps.sort(key=lambda step: _step_sort_key(step["flow_id"]))
    return steps


def extract_pipeline_edges(pipeline_graph: Optional[dict]) -> List[dict]:
    graph = pipeline_graph if isinstance(pipeline_graph, dict) else {}
    edges: List[dict] = []

    raw_edges = graph.get("edges")
    if isinstance(raw_edges, list):
        for edge in raw_edges:
            if not isinstance(edge, dict):
                continue
            source = _clean_string(edge.get("source"))
            target = _clean_string(edge.get("target"))
            if source and target and source != target:
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "source_port": _clean_string(
                            edge.get("sourceHandle") or edge.get("source_port")
                        ),
                        "target_port": _clean_string(
                            edge.get("targetHandle") or edge.get("target_port")
                        ),
                    }
                )

    raw_flows = graph.get("flows")
    if isinstance(raw_flows, list):
        for flow in raw_flows:
            if not isinstance(flow, dict):
                continue
            source = _clean_string(flow.get("source"))
            target = _clean_string(flow.get("target"))
            if source and target and source != target:
                edges.append(
                    {
                        "source": source,
                        "target": target,
                        "source_port": _clean_string(flow.get("source_port")),
                        "target_port": _clean_string(flow.get("target_port")),
                    }
                )

    seen: set[Tuple[str, str, str, str]] = set()
    deduped = []
    for edge in edges:
        key = (
            edge["source"],
            edge["target"],
            edge["source_port"],
            edge["target_port"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _resolved_artifact_bindings(
    steps_by_id: Dict[str, dict],
    edges: Iterable[dict[str, Any]],
) -> list[ArtifactBinding]:
    """Resolve omitted handles to each node's first declared port."""

    resolved_edges: list[dict[str, str]] = []
    for edge in edges:
        source = _clean_string(edge.get("source"))
        target = _clean_string(edge.get("target"))
        if source not in steps_by_id or target not in steps_by_id:
            continue
        source_outputs = (steps_by_id[source].get("ports") or {}).get("outputs") or []
        target_inputs = (steps_by_id[target].get("ports") or {}).get("inputs") or []
        source_port = _clean_string(edge.get("source_port"))
        target_port = _clean_string(edge.get("target_port"))
        if not source_port and source_outputs:
            source_port = _clean_string(source_outputs[0].get("id"))
        if not target_port and target_inputs:
            target_port = _clean_string(target_inputs[0].get("id"))
        resolved_edges.append(
            {
                "source": source,
                "source_port": source_port or "output",
                "target": target,
                "target_port": target_port or "input",
            }
        )
    return artifact_bindings(resolved_edges)


def select_runtime_steps(
    steps: Sequence[dict],
) -> List[dict]:
    return list(steps)


def _validate_flat_task_output_contract(steps: Sequence[dict]) -> None:
    errors: List[str] = []
    for step in steps:
        if _clean_string(step.get("type")).lower() != "task":
            continue
        output_ports = [
            _clean_string(port.get("id"))
            for port in ((step.get("ports") or {}).get("outputs") or [])
            if isinstance(port, dict) and _clean_string(port.get("id"))
        ]
        if len(output_ports) == 1:
            continue
        step_id = _clean_string(step.get("flow_id")) or "<unknown>"
        label = _clean_string(step.get("label"))
        description = f" ({label})" if label else ""
        errors.append(
            f"Task {step_id}{description} declares {len(output_ports)} output ports; "
            "the flat Task workspace supports exactly one logical output set. "
            "Fan out that output to multiple consumers or add an explicit split Task."
        )
    if errors:
        raise DeploymentArtifactValidationError(
            "Flat Task workspace validation failed",
            errors,
        )


def _step_sort_key(flow_id: Any) -> Tuple[int, Any]:
    text = _clean_string(flow_id)
    try:
        return (0, int(text))
    except Exception:
        return (1, text)


def _select_base_image(files: Sequence[dict]) -> str:
    filenames = [entry["filename"].lower() for entry in files]
    if any(name.endswith((".js", ".mjs", ".cjs")) for name in filenames) or "package.json" in filenames:
        return "node:20-slim"
    return "python:3.11-slim"


def _select_command(step: dict) -> List[str]:
    files = step.get("files") or []
    filenames = [entry["filename"] for entry in files]
    preferred_names = (
        "main.py",
        "app.py",
        "run.py",
        "process.py",
        "retrieve.py",
        "notify.py",
    )

    for preferred in preferred_names:
        for filename in filenames:
            if PurePosixPath(filename).name.lower() == preferred:
                return ["python", f"/app/{filename}"]

    for filename in filenames:
        if filename.lower().endswith(".py"):
            return ["python", f"/app/{filename}"]
    for filename in filenames:
        if filename.lower().endswith(".sh"):
            return ["/bin/bash", f"/app/{filename}"]
    for filename in filenames:
        if filename.lower().endswith((".js", ".mjs", ".cjs")):
            return ["node", f"/app/{filename}"]

    label = step.get("label") or f"step {step['flow_id']}"
    return ["python", "-c", f"print('Executing inLUMEN {label}')"]


def _dockerfile_for_step(step: dict) -> dict:
    flow_id = step["flow_id"]
    filename_fragment = _sanitize_fragment(flow_id, "step")
    dockerfile_filename = f"Dockerfile.{filename_fragment}"
    files = list(step.get("files") or [])
    base_image = _select_base_image(files)
    command = _select_command(step)
    image = f"inlumen/{_argo_name(flow_id)}:latest"

    lines = [
        f"FROM {base_image}",
        "ENV PYTHONUNBUFFERED=1",
        "WORKDIR /app",
    ]

    if base_image.startswith("python:"):
        lines.insert(
            1,
            f"COPY --from=ghcr.io/astral-sh/uv:{UV_PINNED_VERSION} /uv /uvx /bin/",
        )

    if any((entry["filename"].lower().endswith(".sh")) for entry in files):
        lines.extend(
            [
                "RUN apt-get update && apt-get install -y --no-install-recommends \\",
                "    bash \\",
                "    && rm -rf /var/lib/apt/lists/*",
            ]
        )

    created_parents: set[str] = set()
    for entry in files:
        source = _safe_docker_source(entry["filename"])
        parent = str(PurePosixPath(source).parent)
        if parent not in ("", ".") and parent not in created_parents:
            created_parents.add(parent)
            lines.append(f"RUN mkdir -p {json.dumps(f'/app/{parent}')}")
        lines.append(f"COPY {_json_array([source, f'/app/{source}'])}")

    filenames = {entry["filename"].lower() for entry in files}
    if "requirements.txt" in filenames:
        lines.append(
            "RUN --mount=type=cache,target=/root/.cache/uv "
            "uv pip install --system -r requirements.txt"
        )
    if "package.json" in filenames:
        lines.append("RUN npm install --omit=dev")
    if any(entry["filename"].lower().endswith(".sh") for entry in files):
        lines.append('RUN find /app -type f -name "*.sh" -exec chmod +x {} \\;')

    lines.extend(
        [
            f'LABEL org.opencontainers.image.title="inLUMEN step {flow_id}"',
            f'LABEL inlumen.flow_id="{flow_id}"',
            f"CMD {_json_array(command)}",
        ]
    )
    return {
        "dockerfile_filename": dockerfile_filename,
        "content": "\n".join(lines) + "\n",
        "flow_id": flow_id,
        "image": image,
        "command": command,
        "files": [entry["filename"] for entry in files],
    }


def build_dockerfile_artifacts(
    pipeline_graph: Optional[dict] = None,
    files: Any = None,
) -> dict:
    """Build baseline Dockerfile artifacts for tests and non-agent guardrail fixtures.

    Deployment Dockerfiles are derived deterministically from registered runtime
    profiles and attached package metadata.
    """
    all_steps = extract_pipeline_steps(pipeline_graph, files)
    steps = select_runtime_steps(all_steps)
    if not steps:
        raise ValueError("No pipeline steps were found for Dockerfile generation.")

    from generators.registry import GeneratorRegistry

    generator_registry = GeneratorRegistry()
    runtime_artifacts = []
    dockerfiles = []
    for step in steps:
        generator = generator_registry.generator_for_step(step)
        if generator is None:
            dockerfiles.append(_dockerfile_for_step(step))
            continue
        bundle = generator.generate(step, pipeline_graph)
        runtime_artifacts.append(bundle.to_dict(include_content=True))
        dockerfiles.append(bundle.dockerfile_artifact())
    validate_dockerfile_artifacts(dockerfiles, [step["flow_id"] for step in steps], steps)
    return {
        "dockerfiles": dockerfiles,
        "runtime_artifacts": runtime_artifacts,
        "guardrails": {
            "valid": True,
            "checks": [
                "one Dockerfile per executable pipeline step",
                "registered deterministic generators bypass the generic runtime inference",
                "Dockerfile filenames match Dockerfile.<step_id>",
                "Dockerfiles include FROM, WORKDIR, build context handling, and CMD",
            ],
        },
    }


def _first_instruction(lines: Sequence[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped.split(None, 1)[0].upper()
    return ""


def _instruction_set(lines: Sequence[str]) -> set[str]:
    instructions = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("&&"):
            continue
        instruction = stripped.split(None, 1)[0].upper()
        if instruction in DOCKERFILE_INSTRUCTIONS:
            instructions.add(instruction)
    return instructions


def validate_dockerfile_artifacts(
    dockerfiles: Any,
    expected_step_ids: Optional[Iterable[str]] = None,
    steps: Optional[Sequence[dict]] = None,
) -> None:
    errors: List[str] = []
    if not isinstance(dockerfiles, list) or not dockerfiles:
        raise DeploymentArtifactValidationError(
            "Dockerfile guardrail validation failed",
            ["dockerfiles must be a non-empty list"],
        )

    expected_ids = {_clean_string(step_id) for step_id in (expected_step_ids or []) if _clean_string(step_id)}
    step_files = {
        _clean_string(step.get("flow_id")): [entry["filename"] for entry in step.get("files") or []]
        for step in (steps or [])
        if isinstance(step, dict)
    }
    steps_by_id = {
        _clean_string(step.get("flow_id")): step
        for step in (steps or [])
        if isinstance(step, dict)
    }
    seen_ids: set[str] = set()

    for idx, artifact in enumerate(dockerfiles):
        if not isinstance(artifact, dict):
            errors.append(f"dockerfiles[{idx}] must be an object")
            continue

        filename = _clean_string(artifact.get("dockerfile_filename"))
        content = _clean_string(artifact.get("content"))
        flow_id = _clean_string(artifact.get("flow_id"))
        match = DOCKERFILE_NAME_RE.match(filename)
        if not match:
            errors.append(f"{filename or f'dockerfiles[{idx}]'} must be named Dockerfile.<step_id>")
        elif not flow_id:
            flow_id = match.group(1)
        elif filename != f"Dockerfile.{_sanitize_fragment(flow_id, 'step')}":
            errors.append(f"{filename} must match step id '{flow_id}'")

        if expected_ids and flow_id not in expected_ids:
            errors.append(f"{filename} references unexpected step id '{flow_id}'")
        if flow_id in seen_ids:
            errors.append(f"duplicate Dockerfile for step id '{flow_id}'")
        seen_ids.add(flow_id)

        if not content:
            errors.append(f"{filename} has empty content")
            continue
        if "```" in content:
            errors.append(f"{filename} contains markdown code fences")

        lines = content.splitlines()
        if _first_instruction(lines) != "FROM":
            errors.append(f"{filename} must start with a FROM instruction")

        instructions = _instruction_set(lines)
        for required in ("FROM", "WORKDIR"):
            if required not in instructions:
                errors.append(f"{filename} is missing required {required} instruction")
        if not ({"CMD", "ENTRYPOINT"} & instructions):
            errors.append(f"{filename} is missing CMD or ENTRYPOINT")

        files_for_step = step_files.get(flow_id, [])
        if files_for_step and not ({"COPY", "ADD"} & instructions):
            errors.append(f"{filename} must COPY or ADD the step files")
        if any(name.lower() == "requirements.txt" for name in files_for_step) and "pip install" not in content:
            errors.append(f"{filename} must install requirements.txt")
        if any(name.lower().endswith(".sh") for name in files_for_step) and "chmod" not in content:
            errors.append(f"{filename} must make shell scripts executable")

    missing = expected_ids - seen_ids
    for step_id in sorted(missing, key=_step_sort_key):
        errors.append(f"missing Dockerfile for step id '{step_id}'")

    if errors:
        raise DeploymentArtifactValidationError("Dockerfile guardrail validation failed", errors)


def _dockerfiles_from_payload(dockerfiles_payload: Any) -> List[dict]:
    if isinstance(dockerfiles_payload, dict):
        value = dockerfiles_payload.get("dockerfiles") or []
    else:
        value = dockerfiles_payload or []
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _flow_id_from_dockerfile_entry(entry: dict) -> str:
    flow_id = _clean_string(entry.get("flow_id") or entry.get("step_id"))
    if flow_id:
        return flow_id
    name = _clean_string(entry.get("dockerfile_filename") or entry.get("name"))
    match = DOCKERFILE_NAME_RE.match(name)
    if match:
        return match.group(1)
    digit_match = re.search(r"(\d+)", name)
    return digit_match.group(1) if digit_match else ""


def _extract_json_cmd_from_dockerfile(content: str) -> List[str]:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("CMD "):
            continue
        raw = stripped[4:].strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except Exception:
                return []
            if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                return parsed
    return []


def _dockerfile_lookup(dockerfiles_payload: Any) -> Dict[str, dict]:
    lookup: Dict[str, dict] = {}
    for entry in _dockerfiles_from_payload(dockerfiles_payload):
        flow_id = _flow_id_from_dockerfile_entry(entry)
        if not flow_id:
            continue
        lookup[flow_id] = entry
    return lookup


def _runtime_command_for_step(
    dockerfiles_payload: Any,
    flow_id: str,
) -> List[str]:
    """Return the declared command for a packaged node, when one is available."""
    dockerfile = _dockerfile_lookup(dockerfiles_payload).get(flow_id, {})
    command = dockerfile.get("command")
    if isinstance(command, list) and all(isinstance(item, str) for item in command):
        return [item for item in command if item.strip()]
    return _extract_json_cmd_from_dockerfile(_clean_string(dockerfile.get("content")))


def _runtime_entrypoint_filename(
    dockerfiles_payload: Any,
    flow_id: str,
) -> str:
    """Resolve a Python entrypoint filename without exposing container paths.

    The bundle can relocate a node from ``/app`` into ``nodes/<node>``.  We keep
    the executable filename from its declared Python command, rather than
    assuming every uploaded package is a directly executable ``main.py``.
    """
    command = _runtime_command_for_step(dockerfiles_payload, flow_id)
    if not command:
        return "main.py"
    candidate = PurePosixPath(command[-1]).name
    if candidate.endswith(".py") and candidate not in {".", ".."}:
        return candidate
    return "main.py"


def _runtime_environment_for_step(
    dockerfiles_payload: Any,
    flow_id: str,
) -> list[dict[str, Any]]:
    entrypoint = _runtime_entrypoint_filename(dockerfiles_payload, flow_id)
    source = _deployment_file_content(dockerfiles_payload, flow_id, entrypoint)
    node_manifest = _json_object(
        _deployment_file_content(
            dockerfiles_payload,
            flow_id,
            "node-manifest.json",
        )
    )
    declared = node_manifest.get("runtime_environment")
    runtime_artifact = _runtime_artifact_for_step(dockerfiles_payload, flow_id)
    generated_source = (
        []
        if _clean_string(runtime_artifact.get("generator"))
        == MANAGED_ADAPTER_GENERATOR
        else discover_runtime_environment(source)
    )
    return merge_runtime_environment(
        generated_source,
        declared,
    )


def _topological_order(step_ids: Sequence[str], edges: Sequence[dict]) -> List[str]:
    step_id_set = set(step_ids)
    incoming = {step_id: 0 for step_id in step_ids}
    outgoing: Dict[str, List[str]] = defaultdict(list)
    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        if source not in step_id_set or target not in step_id_set:
            continue
        outgoing[source].append(target)
        incoming[target] += 1

    ready = deque(sorted([step_id for step_id, count in incoming.items() if count == 0], key=_step_sort_key))
    ordered: List[str] = []
    while ready:
        step_id = ready.popleft()
        ordered.append(step_id)
        for target in sorted(outgoing.get(step_id, []), key=_step_sort_key):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)

    if len(ordered) != len(step_ids):
        raise DeploymentArtifactValidationError(
            "Argo Workflow guardrail validation failed",
            ["pipeline graph contains a cycle and cannot be represented as an Argo DAG"],
        )
    return ordered


def _dependency_lookup(step_ids: Sequence[str], edges: Sequence[dict]) -> Dict[str, List[str]]:
    step_id_set = set(step_ids)
    dependencies: Dict[str, List[str]] = {step_id: [] for step_id in step_ids}
    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        if source in step_id_set and target in step_id_set:
            dependencies[target].append(source)
    for step_id in dependencies:
        dependencies[step_id] = sorted(set(dependencies[step_id]), key=_step_sort_key)
    return dependencies


def _is_current_codegen_step(step: dict) -> bool:
    artifact = step.get("generated_artifact")
    if not isinstance(artifact, dict):
        return False
    if _clean_string(artifact.get("generator")) not in {
        CODEGEN_GENERATOR,
        ATTACHED_RUNTIME_GENERATOR,
        MANAGED_ADAPTER_GENERATOR,
    }:
        return False
    return (_clean_string(artifact.get("status")) or "current").lower() == "current"


def _is_codegen_dockerfile_payload(
    step_ids: Sequence[str],
    dockerfiles_by_step: Dict[str, dict],
) -> bool:
    if not step_ids:
        return False
    for step_id in step_ids:
        dockerfile = dockerfiles_by_step.get(step_id)
        if not isinstance(dockerfile, dict):
            return False
        if _clean_string(dockerfile.get("generator")) not in {
            CODEGEN_GENERATOR,
            ATTACHED_RUNTIME_GENERATOR,
            MANAGED_ADAPTER_GENERATOR,
        }:
            return False
    return True


def _python_identifier(value: Any, fallback: str) -> str:
    text = _clean_string(value).lower()
    text = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    text = re.sub(r"_+", "_", text)
    if not text:
        text = fallback
    if not re.match(r"^[a-z_]", text):
        text = f"{fallback}_{text}"
    return text[:80].rstrip("_") or fallback


def _dagster_asset_names(steps: Sequence[dict]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    used: set[str] = set()
    for step in steps:
        flow_id = _clean_string(step.get("flow_id"))
        step_fragment = re.sub(r"[^a-z0-9_]+", "_", flow_id.lower()).strip("_") or "step"
        label_fragment = re.sub(
            r"[^a-z0-9_]+",
            "_",
            _clean_string(step.get("label")).lower(),
        ).strip("_")
        base_name = f"node_{step_fragment}"
        if label_fragment:
            base_name = f"{base_name}_{label_fragment}"
        base_name = _python_identifier(base_name, "node")
        name = base_name
        suffix = 2
        while name in used:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used.add(name)
        names[flow_id] = name
    return names


def _bundle_node_dir(step: dict) -> str:
    flow_fragment = _sanitize_fragment(step.get("flow_id"), "step")
    label_fragment = _sanitize_fragment(step.get("label"), "")
    if label_fragment:
        return f"node-{flow_fragment}-{label_fragment}"
    return f"node-{flow_fragment}"


def _step_data_contract(step: dict) -> dict:
    artifact = step.get("generated_artifact")
    if not isinstance(artifact, dict):
        return {}
    contract = artifact.get("data_contract")
    return contract if isinstance(contract, dict) else {}


def _contract_env_name(contract: dict, key: str, default: str) -> str:
    value = _clean_string(contract.get(key))
    return value if value else default


def _validate_codegen_argo_shape(
    *,
    bindings: Sequence[ArtifactBinding],
) -> None:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for binding in bindings:
        counts[(binding.target_node, binding.target_port)] += 1
    errors = [
        f"Node {node_id} input port {port_id} has multiple producers; add distinct "
        "input ports or an explicit merge Task before Argo export."
        for (node_id, port_id), count in sorted(counts.items())
        if count > 1
    ]
    if errors:
        raise DeploymentArtifactValidationError(
            "Generated-script Argo Workflow guardrail validation failed",
            errors,
        )


def _build_codegen_argo_workflow_object(
    *,
    steps: Sequence[dict],
    ordered_ids: Sequence[str],
    dependencies: Dict[str, List[str]],
    bindings: Sequence[ArtifactBinding],
    dockerfiles_by_step: Dict[str, dict],
    dockerfiles_payload: Any,
    shared_runtime: Optional[dict] = None,
) -> dict:
    _validate_codegen_argo_shape(bindings=bindings)
    steps_by_id = {step["flow_id"]: step for step in steps}
    bindings_by_target: Dict[str, list[ArtifactBinding]] = defaultdict(list)
    for binding in bindings:
        bindings_by_target[binding.target_node].append(binding)
    child_lookup: Dict[str, List[str]] = {step_id: [] for step_id in ordered_ids}
    for child, parents in dependencies.items():
        for parent in parents:
            child_lookup[parent].append(child)
    leaf_ids = [step_id for step_id in ordered_ids if not child_lookup[step_id]]

    tasks = []
    entry_template = {
        "name": "inlumen-pipeline",
        "dag": {"tasks": tasks},
    }
    workflow_outputs = []
    for leaf_id in leaf_ids:
        output_ports = (steps_by_id[leaf_id].get("ports") or {}).get("outputs") or []
        for port in output_ports:
            port_id = _clean_string(port.get("id"))
            if not port_id:
                continue
            result_name = (
                "result"
                if len(leaf_ids) == 1 and len(output_ports) == 1
                else _argo_name(f"result-{leaf_id}-{port_id}")
            )
            workflow_outputs.append(
                {
                    "name": result_name,
                    "from": (
                        f"{{{{tasks.{_argo_name(leaf_id)}.outputs.artifacts."
                        f"{_argo_name(port_id)}}}}}"
                    ),
                }
            )
    if workflow_outputs:
        entry_template["outputs"] = {
            "artifacts": workflow_outputs
        }

    templates = [entry_template]
    shared_image_parameter = "pipeline-image"
    input_parameters = []
    environment_parameters = []
    seen_environment_parameters = set()
    image_parameters = (
        [
            {
                "name": shared_image_parameter,
                "value": _clean_string((shared_runtime or {}).get("image"))
                or "inlumen/pipeline:latest",
            }
        ]
        if shared_runtime
        else []
    )

    for step_id in ordered_ids:
        parent_ids = dependencies.get(step_id) or []
        step = steps_by_id[step_id]
        incoming_bindings = bindings_by_target.get(step_id) or []
        is_database_source = (
            _clean_string(step.get("type")).lower() == "source"
            and _clean_string(step.get("template")).lower() == "database"
        )
        task = {
            "name": _argo_name(step_id),
            "template": _argo_name(step_id),
        }
        if incoming_bindings:
            task["arguments"] = {
                "artifacts": [
                    {
                        "name": _argo_name(binding.target_port, "input"),
                        "from": (
                            f"{{{{tasks.{_argo_name(binding.source_node)}.outputs.artifacts."
                            f"{_argo_name(binding.source_port, 'output')}}}}}"
                        ),
                    }
                    for binding in incoming_bindings
                ]
            }
        elif not is_database_source:
            input_parameter = _argo_name(f"input-artifact-key-{step_id}")
            input_parameters.append(
                {
                    "name": input_parameter,
                    "value": f"inlumen/input/{_argo_name(step_id)}.tgz",
                }
            )
            task["arguments"] = {
                "artifacts": [{
                    "name": "run-inputs",
                    "s3": {
                        "key": f"{{{{workflow.parameters.{input_parameter}}}}}"
                    },
                }]
            }
        if parent_ids:
            task["dependencies"] = [_argo_name(parent) for parent in parent_ids]
        tasks.append(task)

    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        incoming_bindings = bindings_by_target.get(step_id) or []
        dockerfile = dockerfiles_by_step[step_id]
        image_parameter = (
            shared_image_parameter
            if shared_runtime
            else _argo_name(f"image-{step_id}", "image")
        )
        if not shared_runtime:
            image_parameters.append(
                {
                    "name": image_parameter,
                    "value": dockerfile["image"],
                }
            )

        shared_node = (
            ((shared_runtime or {}).get("nodes") or {}).get(step_id) or {}
        )
        command = shared_node.get("command") or dockerfile.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            command = _extract_json_cmd_from_dockerfile(_clean_string(dockerfile.get("content")))
        if not command:
            command = ["python", "/app/main.py"]

        output_artifacts = []
        output_port_ids = []
        for port in (step.get("ports") or {}).get("outputs") or []:
            port_id = _clean_string(port.get("id"))
            if not port_id:
                continue
            output_port_ids.append(port_id)
            output_artifact = {
                "name": _argo_name(port_id, "output"),
                "path": f"/inlumen/outputs/{port_id}",
                "archive": {"none": {}},
            }
            if step_id in leaf_ids:
                output_artifact["s3"] = {
                    "key": (
                        f"{{{{workflow.parameters.output-artifact-prefix}}}}/"
                        f"{_argo_name(step_id)}/{_argo_name(port_id)}"
                    )
                }
            output_artifacts.append(output_artifact)

        env = [
            {"name": "INLUMEN_FLOW_ID", "value": step_id},
            # The public Task ABI is intentionally just these two directories.
            {"name": "PIPELINE_INPUT_DIR", "value": "/inlumen/inputs"},
            {"name": "PIPELINE_OUTPUT_DIR", "value": "/inlumen/outputs"},
        ]
        if step.get("label"):
            env.append({"name": "INLUMEN_STEP_LABEL", "value": step["label"]})
        if step.get("description"):
            env.append({"name": "INLUMEN_STEP_DESCRIPTION", "value": step["description"]})
        runtime_parameters = _step_runtime_parameters(step)
        if runtime_parameters:
            env.append({
                "name": "INLUMEN_PARAMS_JSON",
                "value": json.dumps(runtime_parameters, ensure_ascii=False, sort_keys=True),
            })
            env.append({
                "name": "PIPELINE_PARAMS_JSON",
                "value": json.dumps(runtime_parameters, ensure_ascii=False, sort_keys=True),
            })
        for key, value in sorted(runtime_parameters.items()):
            env_name = "INLUMEN_PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", key).upper().strip("_")
            if env_name != "INLUMEN_PARAM_":
                env.append({"name": env_name, "value": str(value)})
                env.append({
                    "name": env_name.replace("INLUMEN_", "PIPELINE_"),
                    "value": str(value),
                })
            direct_name = _direct_parameter_environment_name(key)
            if direct_name:
                env.append({"name": direct_name, "value": str(value)})
        for key in _step_secret_parameters(step):
            env_name = "INLUMEN_PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", key).upper().strip("_")
            if env_name != "INLUMEN_PARAM_":
                env.append({
                    "name": env_name,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "inlumen-runtime-secrets",
                            "key": _kubernetes_secret_key(step_id, key),
                        },
                    },
                })
                env.append({
                    "name": env_name.replace("INLUMEN_", "PIPELINE_"),
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "inlumen-runtime-secrets",
                            "key": _kubernetes_secret_key(step_id, key),
                        },
                    },
                })
            direct_name = _direct_parameter_environment_name(key)
            if direct_name:
                env.append({
                    "name": direct_name,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "inlumen-runtime-secrets",
                            "key": _kubernetes_secret_key(step_id, key),
                        },
                    },
                })
        runtime_environment = _runtime_environment_for_step(
            dockerfiles_payload,
            step_id,
        )
        existing_env_names = {item.get("name") for item in env}
        for requirement in runtime_environment:
            name = _clean_string(requirement.get("name"))
            if not name or name in existing_env_names:
                continue
            if requirement.get("secret"):
                env.append({
                    "name": name,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "inlumen-runtime-secrets",
                            "key": _kubernetes_secret_key(step_id, name),
                            "optional": not bool(requirement.get("required")),
                        },
                    },
                })
            else:
                parameter_name = _argo_name(f"env-{step_id}-{name}")
                if parameter_name not in seen_environment_parameters:
                    seen_environment_parameters.add(parameter_name)
                    environment_parameters.append({"name": parameter_name, "value": ""})
                env.append({
                    "name": name,
                    "value": f"{{{{workflow.parameters.{parameter_name}}}}}",
                })
            existing_env_names.add(name)

        annotations = {
            "inlumen.ai/flow-id": step_id,
            "inlumen.ai/type": step.get("type") or "task",
            "inlumen.ai/generator": _clean_string(dockerfile.get("generator"))
            or CODEGEN_GENERATOR,
            "inlumen.ai/dockerfile": (
                _clean_string((shared_runtime or {}).get("dockerfile"))
                or dockerfile["dockerfile_filename"]
            ),
        }
        if dockerfile.get("configuration_hash"):
            annotations["inlumen.ai/configuration-hash"] = dockerfile["configuration_hash"]
        if step.get("label"):
            annotations["inlumen.ai/label"] = step["label"]

        template = {
                "name": _argo_name(step_id),
                "metadata": {"annotations": annotations},
                "container": {
                    "image": f"{{{{workflow.parameters.{image_parameter}}}}}",
                    "imagePullPolicy": "IfNotPresent",
                    "workingDir": _clean_string(shared_node.get("working_dir"))
                    or "/app",
                    "command": [
                        "python",
                        "-c",
                        _ARGO_PORT_RUNNER,
                        json.dumps(command),
                        json.dumps(output_port_ids),
                        json.dumps(runtime_environment),
                        json.dumps([
                            f"/inlumen/staging/{binding.target_port}"
                            for binding in incoming_bindings
                        ]),
                    ],
                    "env": env,
                },
            }
        if output_artifacts:
            template["outputs"] = {"artifacts": output_artifacts}
        if incoming_bindings:
            template["inputs"] = {
                "artifacts": [
                    {
                        "name": _argo_name(binding.target_port, "input"),
                        "path": f"/inlumen/staging/{binding.target_port}",
                    }
                    for binding in incoming_bindings
                ]
            }
        elif not (
            _clean_string(step.get("type")).lower() == "source"
            and _clean_string(step.get("template")).lower() == "database"
        ):
            template["inputs"] = {
                "artifacts": [{"name": "run-inputs", "path": "/inlumen/inputs"}]
            }
        templates.append(template)

    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "generateName": "inlumen-codegen-",
            "labels": {
                "app.kubernetes.io/name": "inlumen-codegen-workflow",
                "app.kubernetes.io/component": "deployment-artifact",
            },
        },
        "spec": {
            "entrypoint": "inlumen-pipeline",
            "artifactRepositoryRef": {
                "configMap": "inlumen-artifact-repositories",
                "key": "minio",
            },
            "arguments": {
                "parameters": [
                    *input_parameters,
                    *environment_parameters,
                    {
                        "name": "output-artifact-prefix",
                        "value": "inlumen/output",
                    },
                    *image_parameters,
                ],
            },
            "templates": templates,
        },
    }


def build_argo_workflow_object(
    pipeline_graph: Optional[dict],
    dockerfiles_payload: Any,
    files: Any = None,
    *,
    shared_runtime: Optional[dict] = None,
) -> dict:
    all_steps = extract_pipeline_steps(pipeline_graph, files)
    if not all_steps:
        raise ValueError("No pipeline steps were found for Argo Workflow generation.")

    edges = extract_pipeline_edges(pipeline_graph)
    steps = select_runtime_steps(all_steps)
    _validate_flat_task_output_contract(steps)
    step_ids = [step["flow_id"] for step in steps]
    dockerfiles = _dockerfiles_from_payload(dockerfiles_payload)
    if not dockerfiles:
        raise ValueError("Dockerfile metadata is required for Argo Workflow generation.")
    validate_dockerfile_artifacts(dockerfiles, step_ids, steps)

    explicit_edges = [
        edge for edge in edges if edge.get("source") in step_ids and edge.get("target") in step_ids
    ]
    if not explicit_edges:
        ordered_ids = [step["flow_id"] for step in steps]
        explicit_edges = [
            {"source": ordered_ids[idx], "target": ordered_ids[idx + 1]}
            for idx in range(len(ordered_ids) - 1)
        ]

    ordered_ids = _topological_order(step_ids, explicit_edges)
    steps_by_id = {step["flow_id"]: step for step in steps}
    dependencies = _dependency_lookup(step_ids, explicit_edges)
    bindings = _resolved_artifact_bindings(steps_by_id, explicit_edges)
    dockerfiles_by_step = _dockerfile_lookup(dockerfiles)

    if steps and (
        all(_is_current_codegen_step(step) for step in steps)
        or _is_codegen_dockerfile_payload(step_ids, dockerfiles_by_step)
    ):
        workflow = _build_codegen_argo_workflow_object(
            steps=steps,
            ordered_ids=ordered_ids,
            dependencies=dependencies,
            bindings=bindings,
            dockerfiles_by_step=dockerfiles_by_step,
            dockerfiles_payload=dockerfiles_payload,
            shared_runtime=shared_runtime,
        )
        validate_argo_workflow_object(workflow, step_ids)
        return workflow

    tasks = []
    templates = [
        {
            "name": "inlumen-pipeline",
            "dag": {
                "tasks": tasks,
            },
        }
    ]

    for step_id in ordered_ids:
        task = {
            "name": _argo_name(step_id),
            "template": _argo_name(step_id),
        }
        dependency_names = [_argo_name(dep) for dep in dependencies.get(step_id, [])]
        if dependency_names:
            task["dependencies"] = dependency_names
        tasks.append(task)

    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        dockerfile = dockerfiles_by_step.get(step_id, {})
        shared_node = (
            ((shared_runtime or {}).get("nodes") or {}).get(step_id) or {}
        )
        image = (
            _clean_string((shared_runtime or {}).get("image"))
            or _clean_string(dockerfile.get("image"))
            or f"inlumen/{_argo_name(step_id)}:latest"
        )
        command = shared_node.get("command") or dockerfile.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            command = _extract_json_cmd_from_dockerfile(_clean_string(dockerfile.get("content")))
        if not command:
            command = _select_command(step)

        env = [
            {"name": "INLUMEN_FLOW_ID", "value": step_id},
            {"name": "INLUMEN_STEP_TYPE", "value": step.get("type") or "task"},
        ]
        if step.get("label"):
            env.append({"name": "INLUMEN_STEP_LABEL", "value": step["label"]})
        if step.get("description"):
            env.append({"name": "INLUMEN_STEP_DESCRIPTION", "value": step["description"]})
        if step.get("endpoint"):
            env.append({"name": "INLUMEN_ENDPOINT", "value": step["endpoint"]})
        if step.get("database"):
            env.append({"name": "INLUMEN_DATABASE", "value": step["database"]})
        if step.get("files"):
            env.append(
                {
                    "name": "INLUMEN_FILES",
                    "value": json.dumps([entry["filename"] for entry in step["files"]]),
                }
            )
        runtime_parameters = _step_runtime_parameters(step)
        if runtime_parameters:
            env.append({
                "name": "INLUMEN_PARAMS_JSON",
                "value": json.dumps(runtime_parameters, ensure_ascii=False, sort_keys=True),
            })
            env.append({
                "name": "PIPELINE_PARAMS_JSON",
                "value": json.dumps(runtime_parameters, ensure_ascii=False, sort_keys=True),
            })
        for key, value in sorted(runtime_parameters.items()):
            env_name = "INLUMEN_PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", key).upper().strip("_")
            if env_name != "INLUMEN_PARAM_":
                env.append({"name": env_name, "value": str(value)})
                env.append({
                    "name": env_name.replace("INLUMEN_", "PIPELINE_"),
                    "value": str(value),
                })
            direct_name = _direct_parameter_environment_name(key)
            if direct_name:
                env.append({"name": direct_name, "value": str(value)})
        for key in _step_secret_parameters(step):
            env_name = "INLUMEN_PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", key).upper().strip("_")
            if env_name != "INLUMEN_PARAM_":
                env.append({
                    "name": env_name,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "inlumen-runtime-secrets",
                            "key": _kubernetes_secret_key(step_id, key),
                        },
                    },
                })
                env.append({
                    "name": env_name.replace("INLUMEN_", "PIPELINE_"),
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "inlumen-runtime-secrets",
                            "key": _kubernetes_secret_key(step_id, key),
                        },
                    },
                })
            direct_name = _direct_parameter_environment_name(key)
            if direct_name:
                env.append({
                    "name": direct_name,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "inlumen-runtime-secrets",
                            "key": _kubernetes_secret_key(step_id, key),
                        },
                    },
                })

        annotations = {
            "inlumen.ai/flow-id": step_id,
            "inlumen.ai/type": step.get("type") or "task",
        }
        if step.get("label"):
            annotations["inlumen.ai/label"] = step["label"]
        if dockerfile.get("dockerfile_filename"):
            annotations["inlumen.ai/dockerfile"] = dockerfile["dockerfile_filename"]

        templates.append(
            {
                "name": _argo_name(step_id),
                "metadata": {"annotations": annotations},
                "container": {
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "workingDir": _clean_string(shared_node.get("working_dir"))
                    or "/app",
                    "command": command,
                    "env": env,
                },
            }
        )

    workflow = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "generateName": "inlumen-pipeline-",
            "labels": {
                "app.kubernetes.io/name": "inlumen-pipeline",
                "app.kubernetes.io/component": "deployment-artifact",
            },
        },
        "spec": {
            "entrypoint": "inlumen-pipeline",
            "templates": templates,
        },
    }
    validate_argo_workflow_object(workflow, step_ids)
    return workflow


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _dump_dict_item(key: str, value: Any, indent: int) -> List[str]:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{key}: {{}}"]
        return [f"{pad}{key}:"] + _dump_yaml_lines(value, indent + 2)
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        return [f"{pad}{key}:"] + _dump_yaml_lines(value, indent + 2)
    return [f"{pad}{key}: {_format_scalar(value)}"]


def _dump_yaml_lines(value: Any, indent: int = 0) -> List[str]:
    pad = " " * indent
    if isinstance(value, dict):
        lines: List[str] = []
        for key, item in value.items():
            lines.extend(_dump_dict_item(str(key), item, indent))
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{pad}- {{}}")
                    continue
                first_key = True
                for key, child in item.items():
                    if first_key:
                        if isinstance(child, (dict, list)):
                            lines.append(f"{pad}- {key}:")
                            lines.extend(_dump_yaml_lines(child, indent + 4))
                        else:
                            lines.append(f"{pad}- {key}: {_format_scalar(child)}")
                        first_key = False
                    else:
                        lines.extend(_dump_dict_item(str(key), child, indent + 2))
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.extend(_dump_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{pad}- {_format_scalar(item)}")
        return lines
    return [f"{pad}{_format_scalar(value)}"]


def dump_yaml(data: dict) -> str:
    return "\n".join(_dump_yaml_lines(data)) + "\n"


def validate_argo_workflow_object(workflow: Any, expected_step_ids: Optional[Iterable[str]] = None) -> None:
    errors: List[str] = []
    if not isinstance(workflow, dict):
        raise DeploymentArtifactValidationError(
            "Argo Workflow guardrail validation failed",
            ["workflow must be an object"],
        )

    if workflow.get("apiVersion") != "argoproj.io/v1alpha1":
        errors.append("apiVersion must be argoproj.io/v1alpha1")
    if workflow.get("kind") != "Workflow":
        errors.append("kind must be Workflow")

    metadata = workflow.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata must be present")
    elif not (metadata.get("name") or metadata.get("generateName")):
        errors.append("metadata.name or metadata.generateName is required")

    spec = workflow.get("spec")
    if not isinstance(spec, dict):
        errors.append("spec must be present")
        spec = {}

    entrypoint = spec.get("entrypoint")
    templates = spec.get("templates")
    if not isinstance(entrypoint, str) or not entrypoint:
        errors.append("spec.entrypoint is required")
    if not isinstance(templates, list) or not templates:
        errors.append("spec.templates must be a non-empty list")
        templates = []

    template_names = [
        template.get("name")
        for template in templates
        if isinstance(template, dict) and template.get("name")
    ]
    duplicate_templates = sorted({name for name in template_names if template_names.count(name) > 1})
    for template_name in duplicate_templates:
        errors.append(f"duplicate template name '{template_name}'")
    template_by_name = {
        template.get("name"): template
        for template in templates
        if isinstance(template, dict) and template.get("name")
    }
    if entrypoint and entrypoint not in template_by_name:
        errors.append(f"entrypoint template '{entrypoint}' is missing")

    entry_template = template_by_name.get(entrypoint, {})
    tasks = ((entry_template.get("dag") or {}).get("tasks") or []) if isinstance(entry_template, dict) else []
    if not isinstance(tasks, list) or not tasks:
        errors.append("entrypoint template must include dag.tasks")
        tasks = []

    task_templates = set()
    task_names = set()
    for task in tasks:
        if not isinstance(task, dict):
            errors.append("dag task must be an object")
            continue
        task_name = task.get("name")
        template_name = task.get("template")
        if not task_name or not template_name:
            errors.append("each dag task requires name and template")
            continue
        if task_name in task_names:
            errors.append(f"duplicate dag task name '{task_name}'")
        task_names.add(task_name)
        task_templates.add(template_name)
        if template_name not in template_by_name:
            errors.append(f"task '{task_name}' references missing template '{template_name}'")

    expected_template_names = {
        _argo_name(step_id)
        for step_id in (expected_step_ids or [])
        if _clean_string(step_id)
    }
    for template_name in sorted(expected_template_names - task_templates):
        errors.append(f"missing dag task for step template '{template_name}'")

    for template_name in task_templates:
        template = template_by_name.get(template_name, {})
        container = template.get("container") if isinstance(template, dict) else None
        script = template.get("script") if isinstance(template, dict) else None
        if not isinstance(container, dict) and not isinstance(script, dict):
            errors.append(f"template '{template_name}' must define container or script")
            continue
        executable = container if isinstance(container, dict) else script
        if not executable.get("image"):
            errors.append(f"template '{template_name}' is missing image")
        if not (executable.get("command") or executable.get("source")):
            errors.append(f"template '{template_name}' is missing command/source")

    if errors:
        raise DeploymentArtifactValidationError("Argo Workflow guardrail validation failed", errors)


def validate_argo_workflow_yaml(
    yaml_text: str,
    expected_step_ids: Optional[Iterable[str]] = None,
) -> None:
    errors: List[str] = []
    if not _clean_string(yaml_text):
        errors.append("YAML content is empty")
    if "```" in yaml_text:
        errors.append("YAML content contains markdown code fences")
    if errors:
        raise DeploymentArtifactValidationError("Argo Workflow guardrail validation failed", errors)

    if yaml is None:
        for required in ("apiVersion:", "kind:", "metadata:", "spec:", "templates:"):
            if required not in yaml_text:
                errors.append(f"YAML content is missing {required}")
        if errors:
            raise DeploymentArtifactValidationError("Argo Workflow guardrail validation failed", errors)
        return

    try:
        docs = list(yaml.safe_load_all(yaml_text))
    except Exception as exc:
        raise DeploymentArtifactValidationError(
            "Argo Workflow guardrail validation failed",
            [f"YAML parsing failed: {exc}"],
        ) from exc

    if len(docs) != 1:
        raise DeploymentArtifactValidationError(
            "Argo Workflow guardrail validation failed",
            ["YAML must contain exactly one document"],
        )
    validate_argo_workflow_object(docs[0], expected_step_ids)


def build_argo_workflow_yaml(
    pipeline_graph: Optional[dict],
    dockerfiles_payload: Any,
    files: Any = None,
    *,
    shared_runtime: Optional[dict] = None,
) -> str:
    steps = extract_pipeline_steps(pipeline_graph, files)
    runtime_steps = select_runtime_steps(steps)
    workflow = build_argo_workflow_object(
        pipeline_graph,
        dockerfiles_payload,
        files,
        shared_runtime=shared_runtime,
    )
    yaml_text = dump_yaml(workflow)
    validate_argo_workflow_yaml(
        yaml_text,
        [step["flow_id"] for step in runtime_steps],
    )
    return yaml_text


def _payload_deployment_files(dockerfiles_payload: Any) -> List[dict]:
    if not isinstance(dockerfiles_payload, dict):
        return []
    files = dockerfiles_payload.get("deployment_files")
    return [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []


def _payload_input_files(dockerfiles_payload: Any) -> List[dict]:
    if not isinstance(dockerfiles_payload, dict):
        return []
    files = dockerfiles_payload.get("input_files")
    return [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []


def _payload_runtime_artifacts(dockerfiles_payload: Any) -> List[dict]:
    if not isinstance(dockerfiles_payload, dict):
        return []
    artifacts = dockerfiles_payload.get("runtime_artifacts")
    return [item for item in artifacts if isinstance(item, dict)] if isinstance(artifacts, list) else []


def _runtime_artifact_for_step(dockerfiles_payload: Any, flow_id: str) -> dict:
    for artifact in _payload_runtime_artifacts(dockerfiles_payload):
        if _clean_string(artifact.get("flow_id")) == flow_id:
            return artifact
    return {}


def _uses_managed_boundary_runtime(step: dict, dockerfiles_payload: Any) -> bool:
    """Identify platform-owned connector runtimes without inspecting code."""
    if str(step.get("type") or "").strip().lower() not in {"source", "destination"}:
        return False
    artifact = _runtime_artifact_for_step(
        dockerfiles_payload,
        _clean_string(step.get("flow_id")),
    )
    generator = _clean_string(artifact.get("generator"))
    # Boundary runtimes are managed unless a Custom node explicitly carries a
    # user-owned attached runtime. AI-generated connector code is not allowed
    # to replace the platform adapter.
    return generator != ATTACHED_RUNTIME_GENERATOR


def _deployment_file_content(
    dockerfiles_payload: Any,
    flow_id: str,
    filename: str,
) -> str:
    for file_entry in _payload_deployment_files(dockerfiles_payload):
        if _clean_string(file_entry.get("flow_id")) != flow_id:
            continue
        if _clean_string(file_entry.get("filename")) == filename:
            return str(file_entry.get("content") or "")

    for artifact in _payload_runtime_artifacts(dockerfiles_payload):
        if _clean_string(artifact.get("flow_id")) != flow_id:
            continue
        for file_entry in artifact.get("files") or []:
            if isinstance(file_entry, dict) and _clean_string(file_entry.get("filename")) == filename:
                return str(file_entry.get("content") or "")
    return ""


def _deployment_files_for_step(dockerfiles_payload: Any, flow_id: str) -> List[dict]:
    files = [
        item
        for item in _payload_deployment_files(dockerfiles_payload)
        if _clean_string(item.get("flow_id")) == flow_id
        and _clean_string(item.get("role")).lower() != "dockerfile"
        and not _clean_string(item.get("filename")).startswith("Dockerfile.")
    ]
    if files:
        return files

    output = []
    for artifact in _payload_runtime_artifacts(dockerfiles_payload):
        if _clean_string(artifact.get("flow_id")) != flow_id:
            continue
        for file_entry in artifact.get("files") or []:
            if (
                isinstance(file_entry, dict)
                and not _clean_string(file_entry.get("filename")).startswith("Dockerfile.")
            ):
                output.append(
                    {
                        "filename": file_entry.get("filename"),
                        "flow_id": flow_id,
                        "content": file_entry.get("content") or "",
                        "content_type": file_entry.get("content_type") or "text/plain",
                        "role": "runtime",
                        **(
                            {"content_encoding": file_entry.get("content_encoding")}
                            if file_entry.get("content_encoding")
                            else {}
                        ),
                        **(
                            {"size_bytes": file_entry.get("size_bytes")}
                            if file_entry.get("size_bytes") is not None
                            else {}
                        ),
                        **(
                            {"sha256": file_entry.get("sha256")}
                            if file_entry.get("sha256")
                            else {}
                        ),
                    }
                )
    return output


def _dagster_file(
    path: str,
    content: str,
    role: str = "dagster",
    *,
    content_type: str = "text/plain;charset=utf-8",
    content_encoding: str = "",
    size_bytes: Any = None,
    sha256: str = "",
) -> dict:
    file_payload = {
        "path": path,
        "filename": PurePosixPath(path).name,
        "flow_id": "",
        "content": content,
        "content_type": content_type,
        "role": role,
    }
    if content_encoding:
        file_payload["content_encoding"] = content_encoding
    if size_bytes is not None:
        file_payload["size_bytes"] = size_bytes
    if sha256:
        file_payload["sha256"] = sha256
    return file_payload


def _dagster_yaml(data: dict) -> str:
    return dump_yaml(data)


def _dagster_shell_command_component_source() -> str:
    # The exported component is intentionally engine-adjacent only: it stages
    # files, starts Python, and inventories produced artifacts.  Task code sees
    # only PIPELINE_INPUT_DIR and PIPELINE_OUTPUT_DIR.
    return filesystem_shell_component_source()


def _legacy_dagster_shell_command_component_source() -> str:
    return '''import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import dagster as dg


TABLE_FORMATS = {"csv", "tsv", "parquet", "xlsx", "xls", "arrow", "feather"}
JSON_FORMATS = {"json", "jsonl", "ndjson"}
TEXT_FORMATS = {"txt", "md", "markdown", "xml", "yaml", "yml", "html", "htm", "rtf", "log"}
IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}


def _canonical_kind(filename, file_format):
    normalized_format = str(file_format or "").strip().lower().lstrip(".")
    if not normalized_format:
        normalized_format = Path(str(filename or "")).suffix.lower().lstrip(".") or "binary"
    if normalized_format in TABLE_FORMATS:
        return "table", normalized_format
    if normalized_format in JSON_FORMATS:
        return "json", normalized_format
    if normalized_format in TEXT_FORMATS:
        return "text", normalized_format
    if normalized_format in IMAGE_FORMATS:
        return "image", normalized_format
    return "binary", normalized_format


def _entry_with_path(entry, manifest_dir, project_root):
    normalized = dict(entry)
    filename = str(
        normalized.get("filename")
        or normalized.get("name")
        or ""
    ).strip()
    raw_path = str(normalized.get("path") or "").strip()
    if raw_path:
        path = Path(raw_path)
        if not path.is_absolute():
            candidates = [
                project_root / path,
                project_root.parent / path,
                manifest_dir / path,
            ]
            if filename:
                candidates.append(manifest_dir / filename)
            path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
        normalized["path"] = str(path)
    elif filename:
        normalized["path"] = str(manifest_dir / filename)
    inferred_kind, normalized_format = _canonical_kind(
        filename,
        normalized.get("format"),
    )
    normalized["format"] = normalized_format
    if str(normalized.get("kind") or "").lower() in {"", "file"}:
        normalized["kind"] = inferred_kind
    return normalized


def _prepare_input_manifest(source_manifest_path: Path, work_dir: Path, project_root: Path) -> Path:
    with source_manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    manifest_dir = source_manifest_path.parent
    if isinstance(manifest.get("inputs"), list):
        inputs = [
            _entry_with_path(entry, manifest_dir, project_root)
            for entry in manifest["inputs"]
            if isinstance(entry, dict)
        ]
    elif isinstance(manifest.get("outputs"), list):
        inputs = [
            _entry_with_path(entry, manifest_dir, project_root)
            for entry in manifest["outputs"]
            if isinstance(entry, dict)
        ]
    elif isinstance(manifest.get("files"), list):
        inputs = [
            _entry_with_path(entry, manifest_dir, project_root)
            for entry in manifest["files"]
            if isinstance(entry, dict)
        ]
    else:
        inputs = []

    prepared_manifest = {
        "schema_version": "inlumen.input-manifest@1",
        "inputs": inputs,
    }
    prepared_path = work_dir / "_dagster_input_manifest.json"
    with prepared_path.open("w", encoding="utf-8") as handle:
        json.dump(prepared_manifest, handle, indent=2)
    return prepared_path


def _quality_gates_from_manifest(output_manifest_path: Path, project_root: Path):
    if not output_manifest_path.is_file():
        return []
    try:
        with output_manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    entries = manifest.get("outputs") or manifest.get("files") or []
    gates = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        normalized = _entry_with_path(
            entry,
            output_manifest_path.parent,
            project_root,
        )
        path = Path(str(normalized.get("path") or ""))
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        quality_gate = payload.get("quality_gate") if isinstance(payload, dict) else None
        if isinstance(quality_gate, dict):
            gates.append({
                "filename": str(normalized.get("filename") or path.name),
                "status": str(quality_gate.get("status") or "unknown").lower(),
                "failures": list(quality_gate.get("failures") or []),
                "warnings": list(quality_gate.get("warnings") or []),
            })
    return gates


def _json_type_matches(value, expected_type):
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _json_schema_errors(value, schema, location):
    if not isinstance(schema, dict):
        return []
    errors = []
    expected_type = schema.get("type")
    if expected_type and not _json_type_matches(value, expected_type):
        return [
            f"{location} must be {expected_type}, got {type(value).__name__}."
        ]
    allowed = schema.get("enum")
    if isinstance(allowed, list) and allowed and value not in allowed:
        errors.append(f"{location} must be one of {allowed!r}.")
    if isinstance(value, dict):
        required = schema.get("required") or []
        missing = [
            str(key)
            for key in required
            if isinstance(key, str) and key not in value
        ]
        if missing:
            errors.append(
                f"{location} is missing required fields: {', '.join(missing)}."
            )
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            for key, property_schema in properties.items():
                if key in value:
                    errors.extend(
                        _json_schema_errors(
                            value[key],
                            property_schema,
                            f"{location}.{key}",
                        )
                    )
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(
                _json_schema_errors(
                    item,
                    schema["items"],
                    f"{location}[{index}]",
                )
            )
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and schema.get("minimum") is not None
        and value < schema["minimum"]
    ):
        errors.append(f"{location} must be at least {schema['minimum']}.")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and schema.get("maximum") is not None
        and value > schema["maximum"]
    ):
        errors.append(f"{location} must be at most {schema['maximum']}.")
    return errors


def _output_contract_errors(
    output_manifest_path: Path,
    context_path: Path | None,
    output_dir: Path,
):
    errors = []
    try:
        with output_manifest_path.open("r", encoding="utf-8") as handle:
            output_manifest = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"Output manifest is unreadable or invalid JSON: {exc}"]

    actual_entries = output_manifest.get("outputs") or output_manifest.get("files")
    if not isinstance(actual_entries, list):
        return ["Output manifest must contain an outputs list."]
    actual_by_name = {}
    for entry in actual_entries:
        if not isinstance(entry, dict):
            errors.append("Output manifest entries must be objects.")
            continue
        name = str(entry.get("name") or Path(str(entry.get("filename") or "")).stem)
        if not name:
            errors.append("Output manifest entry is missing name and filename.")
            continue
        if name in actual_by_name:
            errors.append(f"Output manifest contains duplicate output {name!r}.")
        actual_by_name[name] = entry

    expected_entries = []
    if context_path is not None and context_path.is_file():
        try:
            with context_path.open("r", encoding="utf-8") as handle:
                node_manifest = json.load(handle)
            contract = node_manifest.get("data_contract") or {}
            expected_entries = contract.get("outputs") or []
            if not isinstance(expected_entries, list):
                errors.append("Node data contract outputs must be a list.")
                expected_entries = []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"Node data contract is unreadable or invalid JSON: {exc}")

    expected_by_name = {
        str(entry.get("name") or Path(str(entry.get("filename") or "")).stem): entry
        for entry in expected_entries
        if isinstance(entry, dict)
    }
    for name in sorted(set(actual_by_name) - set(expected_by_name)):
        if expected_by_name:
            errors.append(f"Output manifest contains undeclared output {name!r}.")

    output_root = output_dir.resolve()
    for name, expected in expected_by_name.items():
        actual = actual_by_name.get(name)
        if actual is None:
            errors.append(f"Output manifest is missing declared output {name!r}.")
            continue
        for field in ("filename", "kind", "format"):
            expected_value = str(expected.get(field) or "").strip().lower()
            actual_value = str(actual.get(field) or "").strip().lower()
            if expected_value and actual_value != expected_value:
                errors.append(
                    f"Output {name!r} {field} mismatch: expected "
                    f"{expected_value!r}, got {actual_value or '<missing>'!r}."
                )

        raw_path = str(actual.get("path") or actual.get("filename") or "").strip()
        if not raw_path:
            errors.append(f"Output {name!r} is missing path and filename.")
            continue
        output_path = Path(raw_path)
        if not output_path.is_absolute():
            output_path = output_dir / output_path
        resolved_path = output_path.resolve()
        try:
            resolved_path.relative_to(output_root)
        except ValueError:
            errors.append(
                f"Output {name!r} resolves outside its output directory: "
                f"{resolved_path}."
            )
            continue
        if not resolved_path.is_file():
            errors.append(f"Output file for {name!r} does not exist: {resolved_path}.")
            continue
        if resolved_path.stat().st_size <= 0:
            errors.append(f"Output file for {name!r} is empty.")
            continue

        expected_kind = str(expected.get("kind") or "").strip().lower()
        expected_format = str(expected.get("format") or "").strip().lower()
        if expected_kind == "json" or expected_format in JSON_FORMATS:
            try:
                with resolved_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"JSON output {name!r} is invalid: {exc}")
                continue
            errors.extend(
                _json_schema_errors(
                    payload,
                    expected.get("schema") or {},
                    f"JSON output {name!r}",
                )
            )
    return errors


class ShellCommand(dg.Component, dg.Model, dg.Resolvable):
    asset_key: str
    script_path: str
    upstream_assets: list[str] = []
    input_manifest_path: str
    output_dir: str
    output_manifest_path: str
    context_path: str = ""
    arguments: list[str] = []
    parameters: dict = {}
    secret_environment: dict = {}

    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        deps = [dg.AssetKey(asset_key) for asset_key in self.upstream_assets]

        @dg.asset(name=self.asset_key, deps=deps)
        def run_script(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
            project_root = Path.cwd()
            script_path = Path(self.script_path)
            if not script_path.is_absolute():
                script_path = project_root / script_path

            input_manifest_path = Path(self.input_manifest_path)
            if not input_manifest_path.is_absolute():
                input_manifest_path = project_root / input_manifest_path

            output_dir = Path(self.output_dir)
            if not output_dir.is_absolute():
                output_dir = project_root / output_dir
            output_dir.mkdir(parents=True, exist_ok=True)

            output_manifest_path = Path(self.output_manifest_path)
            if not output_manifest_path.is_absolute():
                output_manifest_path = project_root / output_manifest_path
            output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
            prepared_input_manifest_path = _prepare_input_manifest(
                input_manifest_path,
                output_dir,
                project_root,
            )

            env = dict(os.environ)
            env.update({
                "INLUMEN_FLOW_ID": self.asset_key,
                "INLUMEN_INPUT_MANIFEST": str(prepared_input_manifest_path),
                "INLUMEN_OUTPUT_DIR": str(output_dir),
                "INLUMEN_OUTPUT_MANIFEST": str(output_manifest_path),
            })
            if self.context_path:
                context_path = Path(self.context_path)
                if not context_path.is_absolute():
                    context_path = project_root / context_path
                env["INLUMEN_CONTEXT_PATH"] = str(context_path)
            runtime_parameters = {
                str(key): value
                for key, value in self.parameters.items()
                if str(key).strip() and str(key) != "model_plan"
            }
            if runtime_parameters:
                env["INLUMEN_PARAMS_JSON"] = json.dumps(
                    runtime_parameters,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            for key, value in sorted(runtime_parameters.items()):
                env_name = "INLUMEN_PARAM_" + re.sub(
                    r"[^A-Za-z0-9]+", "_", key
                ).upper().strip("_")
                if env_name != "INLUMEN_PARAM_":
                    env[env_name] = str(value)
            for key, source_env_name in sorted(self.secret_environment.items()):
                target_env_name = "INLUMEN_PARAM_" + re.sub(
                    r"[^A-Za-z0-9]+", "_", str(key)
                ).upper().strip("_")
                if target_env_name == "INLUMEN_PARAM_":
                    continue
                secret_value = os.environ.get(str(source_env_name), "")
                if not secret_value:
                    raise RuntimeError(
                        f"Sensitive parameter {key!r} is not configured for {self.asset_key}."
                    )
                env[target_env_name] = secret_value

            started_at = time.monotonic()
            process = subprocess.Popen(
                [sys.executable, str(script_path), *self.arguments],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            output_queue: queue.Queue = queue.Queue()
            stream_closed = object()
            recent_output = deque(maxlen=400)

            def read_output() -> None:
                assert process.stdout is not None
                try:
                    for raw_line in process.stdout:
                        output_queue.put(raw_line.rstrip())
                finally:
                    output_queue.put(stream_closed)

            output_reader = threading.Thread(
                target=read_output,
                name=f"inlumen-log-{self.asset_key}",
                daemon=True,
            )
            output_reader.start()
            while True:
                try:
                    line = output_queue.get(timeout=15.0)
                except queue.Empty:
                    if process.poll() is None:
                        elapsed = time.monotonic() - started_at
                        context.log.info(
                            f"Node {self.asset_key} is still running "
                            f"({elapsed:.0f}s elapsed)."
                        )
                        continue
                    line = stream_closed
                if line is stream_closed:
                    break
                if line:
                    recent_output.append(line)
                    context.log.info(line)

            returncode = process.wait()
            output_reader.join(timeout=1.0)
            if returncode != 0:
                diagnostic = "\\n".join(recent_output).strip()
                raise RuntimeError(
                    f"Node script {self.asset_key} failed with exit code "
                    f"{returncode}:\\n{diagnostic[-12000:]}"
                )
            if not output_manifest_path.is_file():
                raise RuntimeError(
                    f"Node script {self.asset_key} did not write required output "
                    f"manifest {output_manifest_path}."
                )
            contract_context_path = None
            if self.context_path:
                contract_context_path = Path(self.context_path)
                if not contract_context_path.is_absolute():
                    contract_context_path = project_root / contract_context_path
            contract_errors = _output_contract_errors(
                output_manifest_path,
                contract_context_path,
                output_dir,
            )
            if contract_errors:
                raise RuntimeError(
                    f"Node {self.asset_key} output contract validation failed:\\n- "
                    + "\\n- ".join(contract_errors)
                )
            return dg.MaterializeResult(
                metadata={
                    "output_dir": str(output_dir),
                    "output_manifest_path": str(output_manifest_path),
                }
            )

        @dg.asset_check(asset=run_script, name="quality_policy")
        def quality_policy_check() -> dg.AssetCheckResult:
            project_root = Path.cwd()
            output_manifest_path = Path(self.output_manifest_path)
            if not output_manifest_path.is_absolute():
                output_manifest_path = project_root / output_manifest_path
            gates = _quality_gates_from_manifest(
                output_manifest_path,
                project_root,
            )
            failed = [gate for gate in gates if gate["status"] == "fail"]
            warned = [gate for gate in gates if gate["status"] == "warn"]
            return dg.AssetCheckResult(
                passed=not failed,
                metadata={
                    "quality_gate_count": len(gates),
                    "warning_gate_count": len(warned),
                    "failed_gate_count": len(failed),
                    "quality_gates": dg.MetadataValue.json(gates),
                },
            )

        return dg.Definitions(
            assets=[run_script],
            asset_checks=[quality_policy_check],
        )
'''


def _dagster_readme(
    *,
    asset_names: Sequence[str],
    has_sample_inputs: bool,
    bundle_layout: bool = False,
    has_model_requirements: bool = False,
) -> str:
    input_dir = "inputs" if bundle_layout else "storage/inputs"
    sample_note = (
        f"Run input files were copied into `{input_dir}/`."
        if has_sample_inputs
        else f"No run input files were detected; add files to `{input_dir}/` before materializing root assets."
    )
    run_commands = (
        "```bash\n"
        "docker compose up --build\n"
        "```\n\n"
        "From the exported bundle root, the compose file builds the generated Dagster image, "
        "starts the Dagster webserver, daemon, and isolated code service with PostgreSQL-backed storage, exposes "
        "the UI on port `3000`, and writes materialized task outputs to `outputs/`."
        if bundle_layout
        else "```bash\n"
        "uv run dagster dev -m inlumen_dagster_project.definitions\n"
        "```"
    )
    model_note = (
        "\n\nBefore Dagster starts, the generated `model-prefetch` service acquires "
        "each reviewed model revision into the persistent `inlumen_model_store` "
        "volume, computes a SHA-256 tree manifest, and exits successfully. The "
        "Dagster code service mounts that store read-only and runs model adapters "
        "with external model access disabled. Set `HF_TOKEN` in the launching "
        "environment for authenticated acquisition."
        if has_model_requirements
        else ""
    )
    return f"""# InLumen Dagster Deployment

This project was generated deterministically from persisted InLumen runtime artifacts.

## Assets

{chr(10).join(f"- `{name}`" for name in asset_names)}

## Run Locally

{run_commands}

The reusable component in `src/inlumen_dagster_project/components/shell_command.py` launches each node script as a local subprocess, forwards its logs to Dagster, and creates a standard workspace for every Task:

- `PIPELINE_INPUT_DIR`
- `PIPELINE_OUTPUT_DIR`

Task code reads upstream files directly from the input directory and writes
downstream artifacts directly to the output directory. Port names never create
implicit subdirectories in either public directory. Artifact-owned relative
paths are preserved, and conflicting upstream paths fail before the Task runs.
Dagster inventories and routes files internally; Task code does not read or
write an output manifest.

{sample_note}
{model_note}
"""


def _dagster_project_dependencies(install_requires: Sequence[str]) -> List[str]:
    dependencies = [
        f"dagster=={DAGSTER_PINNED_VERSION}",
        f"dagster-webserver=={DAGSTER_PINNED_VERSION}",
        f"dagster-postgres=={DAGSTER_LIBRARY_PINNED_VERSION}",
        *install_requires,
    ]
    unique_dependencies = []
    seen = set()
    for dependency in dependencies:
        cleaned = dependency.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_dependencies.append(cleaned)
    return unique_dependencies


def _dagster_project_metadata_content(install_requires: Sequence[str]) -> str:
    unique_dependencies = _dagster_project_dependencies(install_requires)
    dependency_lines = ",\n".join(
        f'  "{dependency}"'
        for dependency in unique_dependencies
    )
    return f"""[project]
name = "inlumen-dagster-project"
version = "0.1.0"
description = "Dagster project generated from InLumen deployment artifacts."
requires-python = ">=3.11"
dependencies = [
{dependency_lines}
]

[tool.dagster]
module_name = "inlumen_dagster_project.definitions"
registry_modules = ["inlumen_dagster_project.components"]

[tool.dg]
directory_type = "project"

[tool.dg.project]
root_module = "inlumen_dagster_project"
defs_module = "inlumen_dagster_project.defs"
"""


def _dagster_requirements_content(install_requires: Sequence[str]) -> str:
    return "\n".join(_dagster_project_dependencies(install_requires)) + "\n"


def _parse_requirements_for_dagster_project(content: str) -> List[str]:
    requirements: List[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-", "--")):
            continue
        if line.startswith(("git+", "http://", "https://", "file:", ".")):
            continue
        if "://" in line:
            continue
        if " #" in line:
            line = line.split(" #", 1)[0].strip()
        if line:
            requirements.append(line)
    return requirements


def _model_requirements_for_dagster(
    steps: Sequence[dict],
    dockerfiles_payload: Any,
) -> dict:
    models: List[dict] = []
    seen: set[Tuple[str, str, str]] = set()

    def append_model(
        *,
        flow_id: str,
        adapter_id: str,
        model_id: str,
        model_revision: str,
        model_variants: dict | None = None,
        runtime_selection: dict | None = None,
        artifact_policy: dict | None = None,
    ) -> None:
        key = (adapter_id, model_id, model_revision)
        if key in seen:
            return
        seen.add(key)
        variants = model_variants or {}
        models.append(
            {
                "flow_id": flow_id,
                "adapter_id": adapter_id,
                "model_id": model_id,
                "model_revision": model_revision,
                "model_variants": variants,
                "runtime_selection": runtime_selection or {},
                "profile_env": (
                    "INLUMEN_ASR_PROFILE"
                    if adapter_id == "faster-whisper" and variants
                    else ""
                ),
                "device_env": (
                    "INLUMEN_ASR_DEVICE"
                    if adapter_id == "faster-whisper" and variants
                    else ""
                ),
                "artifact_policy": artifact_policy or {},
            }
        )

    for step in steps:
        flow_id = _clean_string(step.get("flow_id"))
        manifest_content = _deployment_file_content(
            dockerfiles_payload,
            flow_id,
            "node-manifest.json",
        )
        manifest = _json_object(manifest_content)
        declared_models = manifest.get("model_requirements")
        if isinstance(declared_models, list):
            for declared in declared_models:
                if not isinstance(declared, dict) or declared.get("runtime") != "local":
                    continue
                model_id = _clean_string(declared.get("model_id"))
                model_revision = _clean_string(declared.get("model_revision"))
                if model_id and model_revision:
                    append_model(
                        flow_id=flow_id,
                        adapter_id=_clean_string(declared.get("adapter_id")) or "user-declared-model",
                        model_id=model_id,
                        model_revision=model_revision,
                        model_variants=(
                            declared.get("model_variants")
                            if isinstance(declared.get("model_variants"), dict)
                            else {}
                        ),
                        runtime_selection=(
                            declared.get("runtime_selection")
                            if isinstance(declared.get("runtime_selection"), dict)
                            else {}
                        ),
                    )
        plan = (
            manifest.get("implementation_plan")
            if isinstance(manifest.get("implementation_plan"), dict)
            else {}
        )
        artifact_policy = (
            plan.get("artifact_policy")
            if isinstance(plan.get("artifact_policy"), dict)
            else {}
        )
        resolution = (
            plan.get("resolution")
            if isinstance(plan.get("resolution"), dict)
            else {}
        )
        model_id = _clean_string(plan.get("model_id"))
        model_revision = _clean_string(plan.get("model_revision"))
        if not (
            model_id
            and model_revision
            and plan.get("execution_profile") == "trusted_heavy_model"
            and resolution.get("status") == "resolved"
            and artifact_policy.get("schema_version")
            == "inlumen.model-artifact-policy@1"
            and artifact_policy.get("acquisition") == "deployment-preflight"
            and artifact_policy.get("runtime_access") == "verified-local-only"
        ):
            continue
        adapter_id = _clean_string(plan.get("adapter_id"))
        variants = (
            plan.get("model_variants")
            if isinstance(plan.get("model_variants"), dict)
            else {}
        )
        runtime_selection = (
            plan.get("runtime_selection")
            if isinstance(plan.get("runtime_selection"), dict)
            else {}
        )
        append_model(
            flow_id=flow_id,
            adapter_id=adapter_id,
            model_id=model_id,
            model_revision=model_revision,
            model_variants=variants,
            runtime_selection=runtime_selection,
            artifact_policy=artifact_policy,
        )
    return {
        "schema_version": "inlumen.model-requirements@1",
        "models": models,
    }


def _system_requirements_for_dagster(
    steps: Sequence[dict],
    dockerfiles_payload: Any,
) -> list[str]:
    """Collect reviewed OS packages declared by canonical node manifests."""
    allowed = {"ffmpeg"}
    packages: list[str] = []
    for step in steps:
        flow_id = _clean_string(step.get("flow_id"))
        manifest = _json_object(
            _deployment_file_content(
                dockerfiles_payload,
                flow_id,
                "node-manifest.json",
            )
        )
        capabilities = (
            manifest.get("capabilities")
            if isinstance(manifest.get("capabilities"), dict)
            else {}
        )
        dependencies = (
            capabilities.get("dependencies")
            if isinstance(capabilities.get("dependencies"), dict)
            else {}
        )
        for package in dependencies.get("system") or []:
            normalized = _clean_string(package).lower()
            if normalized and normalized not in allowed:
                raise DeploymentArtifactValidationError(
                    "Dagster deployment dependency validation failed",
                    [
                        f"Node {flow_id} requests unsupported system package "
                        f"{normalized!r}."
                    ],
                )
            if normalized and normalized not in packages:
                packages.append(normalized)
    return packages


def _model_prefetch_source() -> str:
    return '''from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

MODEL_MANIFEST_SCHEMA = "inlumen.model-artifact@1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_integrity(snapshot: Path) -> tuple[list[dict], str]:
    files = []
    tree = hashlib.sha256()
    for path in sorted(snapshot.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(snapshot).as_posix()
        size = path.stat().st_size
        sha256 = _sha256_file(path)
        files.append({"path": relative, "size_bytes": size, "sha256": sha256})
        tree.update(f"{sha256} {size} {relative}\\n".encode("utf-8"))
    if not files:
        raise RuntimeError(f"Downloaded model snapshot is empty: {snapshot}")
    return files, tree.hexdigest()


def _safe_snapshot(model_root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if not relative_path or candidate.is_absolute():
        raise RuntimeError("Model manifest contains an unsafe snapshot path.")
    snapshot = (model_root / candidate).resolve()
    try:
        snapshot.relative_to(model_root)
    except ValueError as exc:
        raise RuntimeError("Model snapshot escapes the configured model root.") from exc
    if not snapshot.is_dir():
        raise RuntimeError(f"Model snapshot is missing: {snapshot}")
    return snapshot


def _register_default_revision(model_root: Path, model_id: str, revision: str) -> None:
    """Make a pinned snapshot discoverable by libraries requesting ``main``.

    Some model SDKs accept a friendly model alias and internally request the
    repository's default revision.  The build has already reviewed and pinned
    the exact commit, so this local cache ref maps that alias to the verified
    snapshot without enabling outbound network access.
    """
    repository_cache = (
        model_root
        / "huggingface"
        / ("models--" + model_id.replace("/", "--"))
    )
    refs_dir = repository_cache / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    temporary_ref = refs_dir / "main.tmp"
    # huggingface_hub reads this file verbatim when resolving an alias such as
    # ``main``. Unlike our JSON manifests, the ref must not be newline
    # terminated: the newline becomes part of the revision and prevents the
    # otherwise-present snapshot from being found in offline mode.
    temporary_ref.write_text(revision, encoding="utf-8")
    temporary_ref.replace(refs_dir / "main")


def _existing_verified_artifact(
    model_root: Path,
    model_id: str,
    revision: str,
    spec_sha256: str,
) -> Path | None:
    artifact_dir = model_root / "artifacts" / spec_sha256
    manifest_path = artifact_dir / "inlumen-model-manifest.json"
    verified_path = artifact_dir / "VERIFIED"
    if not manifest_path.is_file() or not verified_path.is_file():
        return None
    manifest_bytes = manifest_path.read_bytes()
    if verified_path.read_text(encoding="utf-8").strip() != hashlib.sha256(
        manifest_bytes
    ).hexdigest():
        return None
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if (
        manifest.get("schema_version") != MODEL_MANIFEST_SCHEMA
        or manifest.get("model_id") != model_id
        or manifest.get("model_revision") != revision
        or manifest.get("spec_sha256") != spec_sha256
    ):
        return None
    snapshot = _safe_snapshot(model_root, str(manifest.get("snapshot_path") or ""))
    if str(os.getenv("INLUMEN_MODEL_VERIFY_ON_START") or "manifest").lower() == "full":
        _, tree_sha256 = _snapshot_integrity(snapshot)
        if tree_sha256 != manifest.get("tree_sha256"):
            return None
    return snapshot


def _selected_specs(requirements: dict) -> list[dict]:
    selected = {}
    accelerator = str(os.getenv("INLUMEN_ACCELERATOR") or "cpu").lower()
    for requirement in requirements.get("models") or []:
        if not isinstance(requirement, dict):
            continue
        model_id = str(requirement.get("model_id") or "").strip()
        revision = str(requirement.get("model_revision") or "").strip()
        variants = requirement.get("model_variants") or {}
        if isinstance(variants, dict) and variants:
            profile_env = str(requirement.get("profile_env") or "")
            requested_profile = str(
                os.getenv(profile_env) if profile_env else ""
            ).strip().lower()
            runtime_selection = requirement.get("runtime_selection") or {}
            if not requested_profile:
                requested_profile = str(
                    runtime_selection.get("default_profile") or "auto"
                ).lower()
            if requested_profile == "auto":
                device_env = str(requirement.get("device_env") or "")
                device = str(os.getenv(device_env) if device_env else "").lower()
                if not device or device == "auto":
                    device = "cuda" if accelerator == "gpu" else "cpu"
                requested_profile = str(
                    (runtime_selection.get("auto_profile_by_device") or {}).get(
                        device,
                        "accuracy",
                    )
                ).lower()
            if requested_profile not in variants:
                raise RuntimeError(
                    f"Unsupported model profile {requested_profile!r} for "
                    f"{requirement.get('adapter_id') or model_id}."
                )
            selected_variant = variants[requested_profile]
            model_id = str(selected_variant.get("model_id") or "").strip()
            revision = str(selected_variant.get("model_revision") or "").strip()
        if not model_id or not revision:
            raise RuntimeError("Reviewed model requirement is missing id or revision.")
        selected[f"{model_id}@{revision}"] = {
            "model_id": model_id,
            "model_revision": revision,
        }
    return [selected[key] for key in sorted(selected)]


def _acquire(model_root: Path, model_id: str, revision: str) -> Path:
    spec_sha256 = hashlib.sha256(f"{model_id}@{revision}".encode("utf-8")).hexdigest()
    existing = _existing_verified_artifact(
        model_root,
        model_id,
        revision,
        spec_sha256,
    )
    if existing is not None:
        _register_default_revision(model_root, model_id, revision)
        print(f"[inlumen:model-prefetch] verified cache hit {model_id}@{revision}", flush=True)
        return existing

    from huggingface_hub import snapshot_download

    print(f"[inlumen:model-prefetch] downloading {model_id}@{revision}", flush=True)
    snapshot = Path(
        snapshot_download(
            repo_id=model_id,
            revision=revision,
            cache_dir=str(model_root / "huggingface"),
            token=os.getenv("HF_TOKEN") or None,
        )
    ).resolve()
    _register_default_revision(model_root, model_id, revision)
    try:
        relative_snapshot = snapshot.relative_to(model_root).as_posix()
    except ValueError as exc:
        raise RuntimeError("Downloaded model snapshot is outside the model root.") from exc
    files, tree_sha256 = _snapshot_integrity(snapshot)
    manifest = {
        "schema_version": MODEL_MANIFEST_SCHEMA,
        "model_id": model_id,
        "model_revision": revision,
        "spec_sha256": spec_sha256,
        "snapshot_path": relative_snapshot,
        "tree_sha256": tree_sha256,
        "files": files,
    }
    artifact_dir = model_root / "artifacts" / spec_sha256
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_dir / "inlumen-model-manifest.json"
    temporary_manifest = artifact_dir / "inlumen-model-manifest.json.tmp"
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\\n"
    ).encode("utf-8")
    temporary_manifest.write_bytes(manifest_bytes)
    temporary_manifest.replace(manifest_path)
    temporary_verified = artifact_dir / "VERIFIED.tmp"
    temporary_verified.write_text(
        hashlib.sha256(manifest_bytes).hexdigest() + "\\n",
        encoding="utf-8",
    )
    temporary_verified.replace(artifact_dir / "VERIFIED")
    print(
        f"[inlumen:model-prefetch] verified {model_id}@{revision} "
        f"tree_sha256={tree_sha256}",
        flush=True,
    )
    return snapshot


def main() -> None:
    model_root = Path(os.getenv("INLUMEN_MODEL_ROOT") or "/models").resolve()
    model_root.mkdir(parents=True, exist_ok=True)
    requirements_path = Path(os.environ["INLUMEN_MODEL_REQUIREMENTS"])
    requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
    if requirements.get("schema_version") != "inlumen.model-requirements@1":
        raise RuntimeError("Unsupported InLUMEN model requirements schema.")
    specs = _selected_specs(requirements)
    for spec in specs:
        _acquire(model_root, spec["model_id"], spec["model_revision"])
    print(
        f"[inlumen:model-prefetch] ready ({len(specs)} model artifact(s))",
        flush=True,
    )


if __name__ == "__main__":
    main()
'''


def _dagster_workspace_content() -> str:
    return """load_from:
  - grpc_server:
      host: dagster-code
      port: 4000
      location_name: inlumen_dagster_project.definitions
"""


def _dagster_dockerfile_content(
    *,
    bundle_layout: bool = False,
    system_packages: Sequence[str] = (),
) -> str:
    system_install = ""
    if system_packages:
        package_lines = " \\\n    ".join(system_packages)
        system_install = (
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n    "
            + package_lines
            + " \\\n    && rm -rf /var/lib/apt/lists/*\n"
        )
    if bundle_layout:
        return f"""# syntax=docker/dockerfile:1.7
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:{UV_PINNED_VERSION} /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1
{system_install}\
ARG INLUMEN_ACCELERATOR=cpu
ARG INLUMEN_PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu
WORKDIR /workspace
COPY dagster/requirements.txt /tmp/inlumen-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \\
    if [ "$INLUMEN_ACCELERATOR" = "cpu" ] && grep -Eiq '^torch([<>=!~].*)?$' /tmp/inlumen-requirements.txt; then \\
         TORCH_REQUIREMENT="$(grep -Ei '^torch([<>=!~].*)?$' /tmp/inlumen-requirements.txt | head -n 1)" \\
         && uv pip install --system --index-url "$INLUMEN_PYTORCH_CPU_INDEX_URL" "$TORCH_REQUIREMENT"; \\
       fi \\
    && uv pip install --system -r /tmp/inlumen-requirements.txt
COPY dagster /workspace/dagster
COPY inputs /workspace/inputs
COPY nodes /workspace/nodes
RUN mkdir -p /workspace/outputs /workspace/dagster/.dagster_home
WORKDIR /workspace/dagster
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system --no-deps -e .
EXPOSE 3000
CMD ["dagster", "dev", "-m", "inlumen_dagster_project.definitions", "-h", "0.0.0.0", "-p", "3000"]
"""
    return f"""# syntax=docker/dockerfile:1.7
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:{UV_PINNED_VERSION} /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1
{system_install}\
ARG INLUMEN_ACCELERATOR=cpu
ARG INLUMEN_PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu
WORKDIR /app
COPY requirements.txt /tmp/inlumen-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \\
    if [ "$INLUMEN_ACCELERATOR" = "cpu" ] && grep -Eiq '^torch([<>=!~].*)?$' /tmp/inlumen-requirements.txt; then \\
         TORCH_REQUIREMENT="$(grep -Ei '^torch([<>=!~].*)?$' /tmp/inlumen-requirements.txt | head -n 1)" \\
         && uv pip install --system --index-url "$INLUMEN_PYTORCH_CPU_INDEX_URL" "$TORCH_REQUIREMENT"; \\
       fi \\
    && uv pip install --system -r /tmp/inlumen-requirements.txt
COPY . /app
RUN mkdir -p /app/.dagster_home
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system --no-deps -e .
EXPOSE 3000
CMD ["dagster", "dev", "-m", "inlumen_dagster_project.definitions", "-h", "0.0.0.0", "-p", "3000"]
"""


def _dagster_instance_yaml_content() -> str:
    return """telemetry:
  enabled: false

run_storage:
  module: dagster_postgres.run_storage
  class: PostgresRunStorage
  config:
    postgres_db:
      hostname:
        env: DAGSTER_POSTGRES_HOST
      username:
        env: DAGSTER_POSTGRES_USER
      password:
        env: DAGSTER_POSTGRES_PASSWORD
      db_name:
        env: DAGSTER_POSTGRES_DB
      port: 5432

event_log_storage:
  module: dagster_postgres.event_log
  class: PostgresEventLogStorage
  config:
    postgres_db:
      hostname:
        env: DAGSTER_POSTGRES_HOST
      username:
        env: DAGSTER_POSTGRES_USER
      password:
        env: DAGSTER_POSTGRES_PASSWORD
      db_name:
        env: DAGSTER_POSTGRES_DB
      port: 5432

schedule_storage:
  module: dagster_postgres.schedule_storage
  class: PostgresScheduleStorage
  config:
    postgres_db:
      hostname:
        env: DAGSTER_POSTGRES_HOST
      username:
        env: DAGSTER_POSTGRES_USER
      password:
        env: DAGSTER_POSTGRES_PASSWORD
      db_name:
        env: DAGSTER_POSTGRES_DB
      port: 5432
"""


def _dagster_compose_content(
    *,
    build_context: str,
    dockerfile: str,
    output_mount: str,
    input_mount: str,
    dagster_home: str,
    workspace_path: str,
    model_prefetch_script: str,
    model_requirements_path: str,
    has_model_requirements: bool,
) -> str:
    input_volume_line = f"  - {input_mount}\n" if input_mount else ""
    model_volume_line = (
        "  - inlumen_model_store:/models:ro\n"
        if has_model_requirements
        else ""
    )
    model_prefetch_service = ""
    model_prefetch_dependency = ""
    model_volume_declaration = ""
    model_download_network = ""
    if has_model_requirements:
        model_prefetch_dependency = """      model-prefetch:
        condition: service_completed_successfully
"""
        model_prefetch_service = f"""
  model-prefetch:
    build: *dagster-build
    command:
      - python
      - {model_prefetch_script}
    init: true
    restart: "no"
    environment:
      HF_HOME: /models/huggingface
      HF_HUB_CACHE: /models/huggingface
      HF_TOKEN: "${{HF_TOKEN:-}}"
      HF_HUB_ETAG_TIMEOUT: "${{HF_HUB_ETAG_TIMEOUT:-30}}"
      HF_HUB_DOWNLOAD_TIMEOUT: "${{HF_HUB_DOWNLOAD_TIMEOUT:-600}}"
      HF_HUB_DISABLE_XET: "${{HF_HUB_DISABLE_XET:-1}}"
      INLUMEN_ACCELERATOR: "${{INLUMEN_ACCELERATOR:-cpu}}"
      INLUMEN_ASR_DEVICE: "${{INLUMEN_ASR_DEVICE:-auto}}"
      INLUMEN_ASR_PROFILE: "${{INLUMEN_ASR_PROFILE:-auto}}"
      INLUMEN_MODEL_ROOT: /models
      INLUMEN_MODEL_REQUIREMENTS: {model_requirements_path}
      INLUMEN_MODEL_VERIFY_ON_START: "${{INLUMEN_MODEL_VERIFY_ON_START:-manifest}}"
      PYTHONUNBUFFERED: "1"
    volumes:
      - inlumen_model_store:/models
    networks:
      - model-download
"""
        model_volume_declaration = """  inlumen_model_store:
    name: "${INLUMEN_MODEL_STORE_VOLUME:-inlumen_model_store}"
"""
        model_download_network = """  model-download:
"""
    return f"""x-dagster-build: &dagster-build
  context: {build_context}
  dockerfile: {dockerfile}
  args:
    INLUMEN_ACCELERATOR: "${{INLUMEN_ACCELERATOR:-cpu}}"
    INLUMEN_PYTORCH_CPU_INDEX_URL: "${{INLUMEN_PYTORCH_CPU_INDEX_URL:-https://download.pytorch.org/whl/cpu}}"

x-dagster-environment: &dagster-environment
  DAGSTER_HOME: {dagster_home}
  DAGSTER_POSTGRES_HOST: dagster-postgres
  DAGSTER_POSTGRES_USER: "${{DAGSTER_POSTGRES_USER:-dagster}}"
  DAGSTER_POSTGRES_PASSWORD: "${{DAGSTER_POSTGRES_PASSWORD:-dagster}}"
  DAGSTER_POSTGRES_DB: "${{DAGSTER_POSTGRES_DB:-dagster}}"
  # Arbitrary user Tasks may acquire their own model or call external services.
  # Reviewed adapters still load their prefetched snapshots directly from
  # INLUMEN_MODEL_ROOT, but do not impose an offline-only policy on user code.
  HF_HUB_OFFLINE: "${{HF_HUB_OFFLINE:-0}}"
  HF_HOME: /models/huggingface
  HF_HUB_CACHE: /models/huggingface
  TRANSFORMERS_OFFLINE: "${{TRANSFORMERS_OFFLINE:-0}}"
  INLUMEN_ACCELERATOR: "${{INLUMEN_ACCELERATOR:-cpu}}"
  INLUMEN_MODEL_ROOT: /models
  INLUMEN_ASR_DEVICE: "${{INLUMEN_ASR_DEVICE:-auto}}"
  INLUMEN_ASR_PROFILE: "${{INLUMEN_ASR_PROFILE:-auto}}"
  INLUMEN_ASR_CPU_THREADS: "${{INLUMEN_ASR_CPU_THREADS:-2}}"
  INLUMEN_ASR_NUM_WORKERS: "${{INLUMEN_ASR_NUM_WORKERS:-1}}"
  PYTHONUNBUFFERED: "1"

x-dagster-volumes: &dagster-volumes
  - {output_mount}
{input_volume_line}\
{model_volume_line}\
  - dagster_compute_logs:{dagster_home}/storage

services:
  dagster-postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: "${{DAGSTER_POSTGRES_USER:-dagster}}"
      POSTGRES_PASSWORD: "${{DAGSTER_POSTGRES_PASSWORD:-dagster}}"
      POSTGRES_DB: "${{DAGSTER_POSTGRES_DB:-dagster}}"
    volumes:
      - dagster_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${{POSTGRES_USER}} -d $${{POSTGRES_DB}}"]
      interval: 5s
      timeout: 5s
      retries: 12
    networks:
      - orchestration
{model_prefetch_service}

  dagster-code:
    build: *dagster-build
    env_file:
      - path: .env
        required: false
    command:
      - dagster
      - api
      - grpc
      - -m
      - inlumen_dagster_project.definitions
      - -h
      - 0.0.0.0
      - -p
      - "4000"
    init: true
    restart: unless-stopped
    stop_grace_period: 30s
    depends_on:
      dagster-postgres:
        condition: service_healthy
{model_prefetch_dependency}\
    volumes: *dagster-volumes
    environment: *dagster-environment
    networks:
      - orchestration
      - runtime-egress
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 4000), timeout=3).close()"]
      interval: 10s
      timeout: 5s
      retries: 18
      start_period: 30s

  dagster-webserver:
    build: *dagster-build
    command:
      - dagster-webserver
      - -w
      - {workspace_path}
      - -h
      - 0.0.0.0
      - -p
      - "3000"
    init: true
    restart: unless-stopped
    stop_grace_period: 30s
    depends_on:
      dagster-postgres:
        condition: service_healthy
      dagster-code:
        condition: service_healthy
    ports:
      - "3000:3000"
    volumes: *dagster-volumes
    environment: *dagster-environment
    networks:
      - orchestration
      - ui
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:3000', timeout=3)"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s

  dagster-daemon:
    build: *dagster-build
    command:
      - dagster-daemon
      - run
      - -w
      - {workspace_path}
    init: true
    restart: unless-stopped
    stop_grace_period: 30s
    depends_on:
      dagster-postgres:
        condition: service_healthy
      dagster-code:
        condition: service_healthy
    volumes: *dagster-volumes
    environment: *dagster-environment
    networks:
      - orchestration

volumes:
  dagster_postgres_data:
  dagster_compute_logs:
{model_volume_declaration}\

networks:
  orchestration:
    internal: true
  ui:
  runtime-egress:
{model_download_network}\
"""


def _dagster_docker_compose_content(
    *,
    bundle_layout: bool = False,
    has_model_requirements: bool = False,
) -> str:
    if bundle_layout:
        return _dagster_compose_content(
            build_context="..",
            dockerfile="dagster/Dockerfile",
            output_mount="../outputs:/workspace/outputs",
            input_mount="../inputs:/workspace/inputs:ro",
            dagster_home="/workspace/dagster/.dagster_home",
            workspace_path="/workspace/dagster/workspace.yaml",
            model_prefetch_script="/workspace/dagster/model_prefetch.py",
            model_requirements_path="/workspace/dagster/model-requirements.json",
            has_model_requirements=has_model_requirements,
        )
    return _dagster_compose_content(
        build_context=".",
        dockerfile="Dockerfile",
        output_mount="./storage:/app/storage",
        input_mount="",
        dagster_home="/app/.dagster_home",
        workspace_path="/app/workspace.yaml",
        model_prefetch_script="/app/model_prefetch.py",
        model_requirements_path="/app/model-requirements.json",
        has_model_requirements=has_model_requirements,
    )


def _bundle_root_docker_compose_content(
    *,
    has_model_requirements: bool = False,
) -> str:
    return _dagster_compose_content(
        build_context=".",
        dockerfile="dagster/Dockerfile",
        output_mount="./outputs:/workspace/outputs",
        input_mount="./inputs:/workspace/inputs:ro",
        dagster_home="/workspace/dagster/.dagster_home",
        workspace_path="/workspace/dagster/workspace.yaml",
        model_prefetch_script="/workspace/dagster/model_prefetch.py",
        model_requirements_path="/workspace/dagster/model-requirements.json",
        has_model_requirements=has_model_requirements,
    )


def _deployment_bundle_readme_content(
    *,
    targets: Dict[str, bool],
    has_model_requirements: bool = False,
    secret_environment_names: Sequence[str] = (),
    runtime_environment: Sequence[dict[str, Any]] = (),
) -> str:
    dagster_section = ""
    if targets.get("dagster"):
        model_note = (
            " A generated model-prefetch service acquires reviewed model revisions "
            "before Dagster starts, verifies a SHA-256 tree manifest, and stores them "
            "in the persistent `inlumen_model_store` volume. Reviewed adapters load "
            "from that read-only local store. Custom Task code may acquire its own "
            "models at runtime; set `HF_TOKEN` before `docker compose up` for "
            "authenticated Hugging Face access."
            if has_model_requirements
            else ""
        )
        dagster_section = f"""
## Dagster

Run the exported bundle directly from this directory:

```bash
docker compose up --build
```

Then open Dagster at `http://localhost:3000` and materialize the generated assets. The generated image pins Dagster to `{DAGSTER_PINNED_VERSION}`, uses `uv`, installs one consolidated dependency set, mounts `inputs/` and `outputs/`, and executes node scripts through `run-spec.json` plus their node manifests. The Compose topology separates the webserver, daemon, and user-code execution service and uses PostgreSQL for concurrent orchestration writes. CPU-only PyTorch is the default; set `INLUMEN_ACCELERATOR=gpu` only for a GPU-capable runtime. ASR defaults to `INLUMEN_ASR_PROFILE=auto`, which selects the pinned multilingual `balanced` model on CPU and the pinned `accuracy` model on CUDA. Set the profile explicitly to `accuracy`, `balanced`, or `fast` when the runtime trade-off is known.{model_note}
"""

    argo_section = ""
    if targets.get("argo"):
        argo_section = """
## Argo

Argo is an optional Kubernetes export, not the local runner. The bundle uses one shared image for the whole compatible Python environment—there is no image per step.

```bash
docker build -f argo/Dockerfile -t YOUR_REGISTRY/inlumen-pipeline:TAG .
docker push YOUR_REGISTRY/inlumen-pipeline:TAG
argo submit argo/workflow.yaml -p pipeline-image=YOUR_REGISTRY/inlumen-pipeline:TAG
```

The workflow still requires an Argo-enabled cluster and an artifact repository. Split images only when nodes genuinely have incompatible dependency environments.
"""

    secrets_section = ""
    if secret_environment_names:
        formatted_names = "\n".join(f"- `{name}`" for name in secret_environment_names)
        secrets_section = f"""
## Sensitive parameters

Copy `.env.example` to `.env` and provide the listed values before local execution. Values are never included in this bundle. For Argo, create the `inlumen-runtime-secrets` Kubernetes Secret using the keys referenced by `argo/workflow.yaml`.

{formatted_names}
"""

    runtime_environment_section = ""
    if runtime_environment:
        formatted_requirements = "\n".join(
            f"- `{item['name']}` ({'required' if item.get('required') else 'optional'})"
            for item in runtime_environment
        )
        runtime_environment_section = f"""
## Task runtime environment

Static analysis found these environment variables in Task code. Required values are checked before the Task process starts; optional values produce a warning when absent.

{formatted_requirements}
"""

    return f"""# InLumen Deployment Bundle

This bundle was generated deterministically from persisted InLumen runtime artifacts.

## Layout

- `inputs/`: files attached to Source nodes
- `nodes/`: per-node runtime source, requirements, and manifests (no node Dockerfiles)
- `outputs/`: per-node output folders used during local Dagster execution
- `run-spec.json`: engine-neutral runtime and filesystem hand-off contract
- `bundle-manifest.json`: machine-readable bundle index
{dagster_section}{argo_section}{runtime_environment_section}{secrets_section}"""


def _dagster_definitions_source() -> str:
    return """from pathlib import Path

import dagster as dg


@dg.definitions
def defs():
    return dg.load_from_defs_folder(project_root=Path(__file__).resolve().parents[2])
"""


def _require_single_parent_handoff(
    ordered_ids: Sequence[str],
    dependencies: Dict[str, List[str]],
) -> None:
    errors = [
        f"Node {step_id} has multiple upstream parents; Dagster script handoff requires an explicit merge node first."
        for step_id in ordered_ids
        if len(dependencies.get(step_id) or []) > 1
    ]
    if errors:
        raise DeploymentArtifactValidationError(
            "Dagster deployment guardrail validation failed",
            errors,
        )


def _root_input_files_for_dagster(
    steps: Sequence[dict],
    dependencies: Dict[str, List[str]],
    dockerfiles_payload: Any,
) -> List[dict]:
    if isinstance(dockerfiles_payload, dict) and "input_files" in dockerfiles_payload:
        return _payload_input_files(dockerfiles_payload)

    # Backward-compatible fallback for bundles generated before input_files
    # became a first-class payload separate from runtime artifacts.
    runtime_filenames = {
        "main.py",
        "requirements.txt",
        "node-manifest.json",
        "validation-report.json",
    }
    input_files: List[dict] = []
    for step in steps:
        flow_id = step["flow_id"]
        if dependencies.get(flow_id):
            continue
        step_files = _deployment_files_for_step(dockerfiles_payload, flow_id)
        node_manifest = _json_object(
            next(
                (
                    str(file_entry.get("content") or "")
                    for file_entry in step_files
                    if _clean_string(file_entry.get("filename"))
                    == "node-manifest.json"
                ),
                "",
            )
        )
        data_contract = (
            node_manifest.get("data_contract")
            if isinstance(node_manifest.get("data_contract"), dict)
            else {}
        )
        contract_inputs = (
            data_contract.get("inputs")
            if isinstance(data_contract.get("inputs"), list)
            else []
        )
        descriptors = {}
        for descriptor in contract_inputs:
            if not isinstance(descriptor, dict):
                continue
            descriptor_filename = _clean_string(
                descriptor.get("filename") or descriptor.get("name")
            )
            if descriptor_filename:
                descriptors[descriptor_filename] = descriptor

        for file_entry in step_files:
            filename = _clean_string(file_entry.get("filename"))
            if not filename:
                continue
            if filename in runtime_filenames or filename.startswith("Dockerfile."):
                continue
            descriptor = descriptors.get(filename, {})
            enriched = dict(file_entry)
            for field in (
                "kind",
                "format",
                "columns",
                "required_columns",
                "schema",
                "semantic_role",
                "description",
            ):
                if descriptor.get(field) not in (None, "", [], {}):
                    enriched[field] = descriptor[field]
            input_files.append(enriched)
    return input_files


def _required_root_input_errors(
    steps: Sequence[dict],
    dependencies: Dict[str, List[str]],
    dockerfiles_payload: Any,
    input_files: Sequence[dict],
) -> List[str]:
    available = {
        (
            _clean_string(item.get("flow_id")),
            _clean_string(item.get("filename")),
        )
        for item in input_files
        if _clean_string(item.get("filename"))
    }
    errors: List[str] = []
    for step in steps:
        flow_id = _clean_string(step.get("flow_id"))
        if dependencies.get(flow_id):
            continue
        step_files = _deployment_files_for_step(dockerfiles_payload, flow_id)
        node_manifest = _json_object(
            next(
                (
                    str(file_entry.get("content") or "")
                    for file_entry in step_files
                    if _clean_string(file_entry.get("filename"))
                    == "node-manifest.json"
                ),
                "",
            )
        )
        contract = (
            node_manifest.get("data_contract")
            if isinstance(node_manifest.get("data_contract"), dict)
            else {}
        )
        for descriptor in contract.get("inputs") or []:
            if not isinstance(descriptor, dict):
                continue
            filename = _clean_string(
                descriptor.get("filename") or descriptor.get("name")
            )
            if filename and (flow_id, filename) not in available:
                errors.append(
                    f"Root node {flow_id} requires input {filename}, but it was "
                    "not packaged from InLumen storage."
                )
    return errors


def _validate_explicit_input_integrity(
    dockerfiles_payload: Any,
    input_files: Sequence[dict],
) -> None:
    if not (
        isinstance(dockerfiles_payload, dict)
        and "input_files" in dockerfiles_payload
    ):
        return

    errors: List[str] = []
    for file_entry in input_files:
        filename = _clean_string(file_entry.get("filename")) or "<unnamed>"
        if file_entry.get("size_bytes") is None:
            errors.append(f"Input {filename} is missing size_bytes.")
        if not _clean_string(file_entry.get("sha256")):
            errors.append(f"Input {filename} is missing sha256.")
        try:
            content = decode_artifact_content(file_entry)
            verify_artifact_integrity(file_entry, content)
        except Exception as exc:
            errors.append(f"Input {filename} failed integrity validation: {exc}")
    if errors:
        raise DeploymentArtifactValidationError(
            "Deployment input guardrail validation failed",
            errors,
        )


def _root_input_owner(
    file_entry: dict,
    root_step_ids: Sequence[str],
) -> str:
    owner = _clean_string(file_entry.get("flow_id"))
    if owner in root_step_ids:
        return owner
    if len(root_step_ids) == 1:
        return root_step_ids[0]
    filename = _clean_string(file_entry.get("filename")) or "<unnamed>"
    raise DeploymentArtifactValidationError(
        "Deployment input ownership validation failed",
        [
            f"Input {filename} must identify one root Source with flow_id; "
            f"available roots are {', '.join(root_step_ids)}."
        ],
    )


def _manifest_input_entry(file_entry: dict, *, path_prefix: str) -> dict:
    filename = _clean_string(file_entry.get("filename"))
    classification = classify_artifact(
        filename,
        kind=file_entry.get("kind"),
        file_format=file_entry.get("format"),
    )
    return {
        "filename": filename,
        "path": f"{path_prefix}/{_safe_docker_source(filename)}",
        **classification,
        "description": _clean_string(file_entry.get("description"))
        or "Input file supplied for this execution.",
        **(
            {"columns": list(file_entry.get("columns") or [])}
            if file_entry.get("columns")
            else {}
        ),
        **(
            {"required_columns": list(file_entry.get("required_columns") or [])}
            if file_entry.get("required_columns")
            else {}
        ),
        **(
            {"schema": file_entry.get("schema")}
            if isinstance(file_entry.get("schema"), dict)
            and file_entry.get("schema")
            else {}
        ),
        **(
            {"semantic_role": _clean_string(file_entry.get("semantic_role"))}
            if _clean_string(file_entry.get("semantic_role"))
            else {}
        ),
        **(
            {"size_bytes": file_entry.get("size_bytes")}
            if file_entry.get("size_bytes") is not None
            else {}
        ),
        **(
            {"sha256": _clean_string(file_entry.get("sha256"))}
            if _clean_string(file_entry.get("sha256"))
            else {}
        ),
    }


def _input_manifest_for_dagster(input_files: Sequence[dict]) -> str:
    manifest = {
        "schema_version": "inlumen.input-manifest@1",
        "inputs": [
            _manifest_input_entry(file_entry, path_prefix="storage/inputs")
            for file_entry in input_files
            if _clean_string(file_entry.get("filename"))
        ]
    }
    return json.dumps(manifest, indent=2) + "\n"


def build_dagster_project_files(
    pipeline_graph: Optional[dict],
    dockerfiles_payload: Any,
    files: Any = None,
    *,
    project_dir: str = "dagster_project",
    bundle_layout: bool = False,
) -> List[dict]:
    all_steps = extract_pipeline_steps(pipeline_graph, files)
    if not all_steps:
        raise ValueError("No pipeline steps were found for Dagster project generation.")

    edges = extract_pipeline_edges(pipeline_graph)
    steps = select_runtime_steps(all_steps)
    _validate_flat_task_output_contract(steps)
    step_ids = [step["flow_id"] for step in steps]
    dockerfiles = _dockerfiles_from_payload(dockerfiles_payload)
    if not dockerfiles:
        raise ValueError("Dockerfile metadata is required for Dagster project generation.")
    validate_dockerfile_artifacts(dockerfiles, step_ids, steps)

    explicit_edges = [
        edge for edge in edges if edge.get("source") in step_ids and edge.get("target") in step_ids
    ]
    if not explicit_edges:
        explicit_edges = [
            {"source": step_ids[idx], "target": step_ids[idx + 1]}
            for idx in range(len(step_ids) - 1)
        ]

    ordered_ids = _topological_order(step_ids, explicit_edges)
    dependencies = _dependency_lookup(step_ids, explicit_edges)

    steps_by_id = {step["flow_id"]: step for step in steps}
    bindings = _resolved_artifact_bindings(steps_by_id, explicit_edges)
    bindings_by_target: Dict[str, list[ArtifactBinding]] = defaultdict(list)
    for binding in bindings:
        bindings_by_target[binding.target_node].append(binding)
    asset_names = _dagster_asset_names([steps_by_id[step_id] for step_id in ordered_ids])
    model_requirements = _model_requirements_for_dagster(
        [steps_by_id[step_id] for step_id in ordered_ids],
        dockerfiles_payload,
    )
    has_model_requirements = bool(model_requirements["models"])
    system_requirements = _system_requirements_for_dagster(
        [steps_by_id[step_id] for step_id in ordered_ids],
        dockerfiles_payload,
    )
    output_files: List[dict] = []
    aggregate_requirements: List[str] = []
    project_dir = _sanitize_fragment(project_dir, "dagster_project")

    root_input_files = _root_input_files_for_dagster(steps, dependencies, dockerfiles_payload)
    input_errors = _required_root_input_errors(
        steps,
        dependencies,
        dockerfiles_payload,
        root_input_files,
    )
    if input_errors:
        raise DeploymentArtifactValidationError(
            "Dagster deployment input validation failed",
            input_errors,
        )
    _validate_explicit_input_integrity(dockerfiles_payload, root_input_files)
    if not bundle_layout:
        root_step_ids = [
            step_id for step_id in ordered_ids if not dependencies.get(step_id)
        ]
        for file_entry in root_input_files:
            owner = _root_input_owner(file_entry, root_step_ids)
            filename = _safe_docker_source(_clean_string(file_entry.get("filename")))
            output_files.append(
                _dagster_file(
                    f"{project_dir}/storage/inputs/{asset_names[owner]}/{filename}",
                    str(file_entry.get("content") or ""),
                    "dagster-input",
                    content_type=str(
                        file_entry.get("content_type")
                        or "application/octet-stream"
                    ),
                    content_encoding=_clean_string(
                        file_entry.get("content_encoding")
                    ),
                    size_bytes=file_entry.get("size_bytes"),
                    sha256=_clean_string(file_entry.get("sha256")),
                )
            )
    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        asset_name = asset_names[step_id]
        node_dir = _bundle_node_dir(step)
        entrypoint_filename = _runtime_entrypoint_filename(
            dockerfiles_payload,
            step_id,
        )
        script_content = _deployment_file_content(
            dockerfiles_payload,
            step_id,
            entrypoint_filename,
        )
        if not script_content:
            raise DeploymentArtifactValidationError(
                "Dagster deployment guardrail validation failed",
                [
                    f"Node {step_id} is missing {entrypoint_filename} in persisted "
                    "runtime artifacts."
                ],
            )

        requirements_content = _deployment_file_content(
            dockerfiles_payload,
            step_id,
            "requirements.txt",
        )
        aggregate_requirements.extend(
            _parse_requirements_for_dagster_project(requirements_content)
        )

        node_artifact_root = f"{project_dir}/src/inlumen_dagster_project/artifacts/nodes/{_sanitize_fragment(step_id, 'step')}"
        script_root = f"{project_dir}/src/inlumen_dagster_project/scripts/{asset_name}"
        defs_root = f"{project_dir}/src/inlumen_dagster_project/defs/{asset_name}"
        if not bundle_layout:
            output_files.append(
                _dagster_file(
                    f"{script_root}/{entrypoint_filename}",
                    script_content,
                    "dagster-script",
                )
            )
            # Function-style uploads are library modules.  Mirror their Python
            # files next to the launcher so sibling imports work in the
            # non-bundle Dagster project too.
            for file_entry in _deployment_files_for_step(
                dockerfiles_payload,
                step_id,
            ):
                filename = _clean_string(file_entry.get("filename"))
                if (
                    not filename.endswith(".py")
                    or filename == entrypoint_filename
                ):
                    continue
                output_files.append(
                    _dagster_file(
                        f"{script_root}/{_safe_docker_source(filename)}",
                        str(file_entry.get("content") or ""),
                        "dagster-script",
                        content_type=str(
                            file_entry.get("content_type")
                            or "text/x-python;charset=utf-8"
                        ),
                        content_encoding=_clean_string(
                            file_entry.get("content_encoding")
                        ),
                        size_bytes=file_entry.get("size_bytes"),
                        sha256=_clean_string(file_entry.get("sha256")),
                    )
                )

            for file_entry in _deployment_files_for_step(dockerfiles_payload, step_id):
                filename = _clean_string(file_entry.get("filename"))
                if not filename:
                    continue
                output_files.append(
                    _dagster_file(
                        f"{node_artifact_root}/{_safe_docker_source(filename)}",
                        str(file_entry.get("content") or ""),
                        "dagster-node-artifact",
                        content_type=str(
                            file_entry.get("content_type")
                            or "application/octet-stream"
                        ),
                        content_encoding=_clean_string(
                            file_entry.get("content_encoding")
                        ),
                        size_bytes=file_entry.get("size_bytes"),
                        sha256=_clean_string(file_entry.get("sha256")),
                    )
                )

        parents = dependencies.get(step_id) or []
        incoming_bindings = bindings_by_target.get(step_id) or []
        input_bindings = [
            {
                "source_dir": (
                    f"../outputs/{_bundle_node_dir(steps_by_id[binding.source_node])}"
                    if bundle_layout
                    else f"storage/{asset_names[binding.source_node]}"
                ),
                "source_port": binding.source_port,
                "target_port": binding.target_port,
                "run_scoped": True,
                "required": True,
            }
            for binding in incoming_bindings
        ]
        if not input_bindings:
            input_bindings = [
                {
                    "source_dir": (
                        f"../inputs/{node_dir}"
                        if bundle_layout
                        else f"storage/inputs/{asset_name}"
                    ),
                    "source_port": "",
                    "target_port": "",
                    "run_scoped": False,
                    "required": False,
                }
            ]
        output_ports = [
            _clean_string(port.get("id"))
            for port in ((step.get("ports") or {}).get("outputs") or [])
            if _clean_string(port.get("id"))
        ]
        input_dir = (
            f"../workspaces/{node_dir}/input"
            if bundle_layout
            else f"storage/{asset_name}/input"
        )
        output_dir = (
            f"../outputs/{node_dir}"
            if bundle_layout
            else f"storage/{asset_name}"
        )
        script_path = (
            f"../nodes/{node_dir}/{entrypoint_filename}"
            if bundle_layout
            else f"src/inlumen_dagster_project/scripts/{asset_name}/{entrypoint_filename}"
        )
        context_path = (
            f"../nodes/{node_dir}/node-manifest.json"
            if bundle_layout
            else f"src/inlumen_dagster_project/artifacts/nodes/{_sanitize_fragment(step_id, 'step')}/node-manifest.json"
        )
        secret_parameter_names = _runtime_secret_parameters(step, dockerfiles_payload)
        runtime_environment = _runtime_environment_for_step(
            dockerfiles_payload,
            step_id,
        )
        defs_yaml = _dagster_yaml(
            {
                "type": "inlumen_dagster_project.components.shell_command.ShellCommand",
                "attributes": {
                    "asset_key": asset_name,
                    "script_path": script_path,
                    "upstream_assets": [asset_names[parent] for parent in parents],
                    "input_bindings": input_bindings,
                    "input_dir": input_dir,
                    "output_dir": output_dir,
                    "output_ports": output_ports,
                    "arguments": [],
                    "parameters": {
                        str(key): value
                        for key, value in _step_runtime_parameters(step).items()
                    },
                    "secret_environment": {
                        key: runtime_secret_name(step_id, key)
                        for key in secret_parameter_names
                    },
                    "runtime_environment": runtime_environment,
                },
            }
        )
        output_files.append(_dagster_file(f"{defs_root}/defs.yaml", defs_yaml, "dagster-defs"))

    asset_name_list = [asset_names[step_id] for step_id in ordered_ids]
    output_files.extend(
        [
            _dagster_file(
                f"{project_dir}/pyproject.toml",
                _dagster_project_metadata_content(aggregate_requirements),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/requirements.txt",
                _dagster_requirements_content(aggregate_requirements),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/Dockerfile",
                _dagster_dockerfile_content(
                    bundle_layout=bundle_layout,
                    system_packages=system_requirements,
                ),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/docker-compose.yml",
                _dagster_docker_compose_content(
                    bundle_layout=bundle_layout,
                    has_model_requirements=has_model_requirements,
                ),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/README.md",
                _dagster_readme(
                    asset_names=asset_name_list,
                    has_sample_inputs=bool(root_input_files),
                    bundle_layout=bundle_layout,
                    has_model_requirements=has_model_requirements,
                ),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/workspace.yaml",
                _dagster_workspace_content(),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/.dagster_home/dagster.yaml",
                _dagster_instance_yaml_content(),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/src/inlumen_dagster_project/__init__.py",
                "",
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/src/inlumen_dagster_project/definitions.py",
                _dagster_definitions_source(),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/src/inlumen_dagster_project/components/__init__.py",
                "from .shell_command import ShellCommand\n",
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/src/inlumen_dagster_project/components/shell_command.py",
                _dagster_shell_command_component_source(),
                "dagster-project",
            ),
            _dagster_file(
                f"{project_dir}/deployment-manifest.json",
                json.dumps(
                    {
                        "schema_version": "inlumen.dagster-deployment@1",
                        "asset_order": asset_name_list,
                        "source": "inlumen deployment artifacts",
                        "bundle_layout": bundle_layout,
                        "model_artifact_count": len(model_requirements["models"]),
                    },
                    indent=2,
                )
                + "\n",
                "dagster-project",
            ),
        ]
    )
    if has_model_requirements:
        output_files.extend(
            [
                _dagster_file(
                    f"{project_dir}/model-requirements.json",
                    json.dumps(model_requirements, indent=2, sort_keys=True) + "\n",
                    "dagster-model-requirements",
                ),
                _dagster_file(
                    f"{project_dir}/model_prefetch.py",
                    _model_prefetch_source(),
                    "dagster-model-prefetch",
                ),
            ]
        )
    return output_files


def _bundle_file(
    path: str,
    content: str,
    *,
    role: str,
    flow_id: str = "",
    content_type: str = "text/plain;charset=utf-8",
    content_encoding: str = "",
    size_bytes: Any = None,
    sha256: str = "",
) -> dict:
    file_payload = {
        "path": path,
        "filename": PurePosixPath(path).name,
        "flow_id": flow_id,
        "content": content,
        "content_type": content_type,
        "role": role,
    }
    if content_encoding:
        file_payload["content_encoding"] = content_encoding
    if size_bytes is not None:
        file_payload["size_bytes"] = size_bytes
    if sha256:
        file_payload["sha256"] = sha256
    return file_payload


def _bundle_input_manifest(input_files: Sequence[dict]) -> str:
    manifest = {
        "schema_version": "inlumen.input-manifest@1",
        "inputs": [
            _manifest_input_entry(file_entry, path_prefix="inputs")
            for file_entry in input_files
            if _clean_string(file_entry.get("filename"))
        ],
    }
    return json.dumps(manifest, indent=2) + "\n"


def _shared_argo_runtime(
    steps: Sequence[dict],
    ordered_ids: Sequence[str],
    dockerfiles_payload: Any,
) -> dict:
    """Describe one Python image that can execute every node in the workflow."""
    steps_by_id = {step["flow_id"]: step for step in steps}
    requirements: List[str] = []
    digest_entries: List[dict] = []
    nodes: dict[str, dict] = {}

    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        node_dir = _bundle_node_dir(step)
        step_files = _deployment_files_for_step(dockerfiles_payload, step_id)
        filenames = {
            _clean_string(file_entry.get("filename"))
            for file_entry in step_files
        }
        missing_runtime_files = [
            filename
            for filename in ("main.py", "node-manifest.json")
            if filename not in filenames
        ]
        if missing_runtime_files:
            raise DeploymentArtifactValidationError(
                "Shared Argo runtime validation failed",
                [
                    f"Node {step_id} is missing {', '.join(missing_runtime_files)}; "
                    "the shared runtime requires one deterministic entrypoint and "
                    "manifest per node."
                ],
            )
        requirements.extend(
            _parse_requirements_for_dagster_project(
                _deployment_file_content(
                    dockerfiles_payload,
                    step_id,
                    "requirements.txt",
                )
            )
        )
        for file_entry in step_files:
            content = str(file_entry.get("content") or "")
            digest_entries.append(
                {
                    "flow_id": step_id,
                    "filename": _clean_string(file_entry.get("filename")),
                    "sha256": _clean_string(file_entry.get("sha256"))
                    or hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
            )
        working_dir = f"/workspace/nodes/{node_dir}"
        entrypoint_filename = _runtime_entrypoint_filename(
            dockerfiles_payload,
            step_id,
        )
        nodes[step_id] = {
            "working_dir": working_dir,
            "command": ["python", f"{working_dir}/{entrypoint_filename}"],
            "context_path": f"{working_dir}/node-manifest.json",
        }

    unique_requirements: List[str] = []
    seen_requirements: set[str] = set()
    for requirement in requirements:
        cleaned = requirement.strip()
        key = cleaned.lower()
        if not cleaned or key in seen_requirements:
            continue
        seen_requirements.add(key)
        unique_requirements.append(cleaned)
    environment_hash = hashlib.sha256(
        json.dumps(
            {
                "python": "3.11",
                "requirements": unique_requirements,
                "files": sorted(
                    digest_entries,
                    key=lambda item: (item["flow_id"], item["filename"]),
                ),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    image = f"inlumen/pipeline:{environment_hash[:12]}"
    requirements_content = "\n".join(unique_requirements)
    if requirements_content:
        requirements_content += "\n"
    dockerfile = f"""# syntax=docker/dockerfile:1.7
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:{UV_PINNED_VERSION} /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1
WORKDIR /workspace
COPY argo/requirements.txt /tmp/inlumen-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \\
    if [ -s /tmp/inlumen-requirements.txt ]; then \\
      uv pip install --system -r /tmp/inlumen-requirements.txt; \\
    fi
COPY nodes /workspace/nodes
LABEL inlumen.runtime.environment-hash="{environment_hash}"
"""
    return {
        "strategy": "shared",
        "image": image,
        "environment_hash": environment_hash,
        "dockerfile": "argo/Dockerfile",
        "requirements": "argo/requirements.txt",
        "dockerfile_content": dockerfile,
        "requirements_content": requirements_content,
        "nodes": nodes,
    }


def build_run_spec(
    *,
    steps: Sequence[dict],
    ordered_ids: Sequence[str],
    connections: Sequence[dict],
    dependencies: Dict[str, List[str]],
    dockerfiles_payload: Any,
    targets: dict,
    shared_argo_runtime: Optional[dict] = None,
) -> dict:
    """Build the engine-neutral execution contract consumed by every adapter."""
    steps_by_id = {step["flow_id"]: step for step in steps}
    resolved_connections = [
        {
            "source": binding.source_node,
            "source_port": binding.source_port,
            "target": binding.target_node,
            "target_port": binding.target_port,
        }
        for binding in _resolved_artifact_bindings(steps_by_id, connections)
    ]
    nodes = []
    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        node_dir = _bundle_node_dir(step)
        entrypoint_filename = _runtime_entrypoint_filename(
            dockerfiles_payload,
            step_id,
        )
        requirements = _parse_requirements_for_dagster_project(
            _deployment_file_content(
                dockerfiles_payload,
                step_id,
                "requirements.txt",
            )
        )
        dependency_hash = hashlib.sha256(
            json.dumps(
                {"python": "3.11", "requirements": requirements},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        ports = step.get("ports") or normalize_node_ports(
            None,
            step.get("type") or "task",
        )
        node_kind = step.get("type") or "task"
        managed_boundary = _uses_managed_boundary_runtime(step, dockerfiles_payload)
        executable_as_package = node_kind == "task" or not managed_boundary
        node_manifest = (
            _json_object(
                _deployment_file_content(
                    dockerfiles_payload,
                    step_id,
                    "node-manifest.json",
                )
            )
            if executable_as_package
            else {}
        )
        secret_parameter_names = _runtime_secret_parameters(step, dockerfiles_payload)
        runtime_environment = _runtime_environment_for_step(
            dockerfiles_payload,
            step_id,
        )
        node_entry = {
            "id": step_id,
            "label": step.get("label") or "",
            "kind": node_kind,
            "template": step.get("template") or "",
            "execution": (
                {
                    "kind": "python-package",
                    "ownership": "user",
                }
                if executable_as_package
                else {
                    "kind": "managed-adapter",
                    "ownership": "inlumen",
                    "adapter": node_kind,
                }
            ),
            "inputs": ports.get("inputs") or [],
            "outputs": ports.get("outputs") or [],
            "parents": dependencies.get(step_id) or [],
            "output_path": f"outputs/{node_dir}",
            "parameters": _step_runtime_parameters(step),
            "secret_parameters": [
                {
                    "name": key,
                    "environment": runtime_secret_name(step_id, key),
                    "argo_secret_key": _kubernetes_secret_key(step_id, key),
                }
                for key in secret_parameter_names
            ],
            "runtime_environment": runtime_environment,
        }
        if executable_as_package:
            node_entry["package"] = {
                "path": f"nodes/{node_dir}",
                "entrypoint": f"nodes/{node_dir}/{entrypoint_filename}",
                "manifest": f"nodes/{node_dir}/node-manifest.json",
                "requirements": f"nodes/{node_dir}/requirements.txt",
                "command": ["python", f"nodes/{node_dir}/{entrypoint_filename}"],
                "package_manager": "uv",
                "python": "3.11",
                "environment_hash": dependency_hash,
            }
            if isinstance(node_manifest.get("io_contract"), dict):
                node_entry["io_contract"] = node_manifest["io_contract"]
            if isinstance(node_manifest.get("capabilities"), dict):
                node_entry["capabilities"] = node_manifest["capabilities"]
        else:
            node_entry["adapter"] = {
                "template": step.get("template") or "",
                "parameters": _step_runtime_parameters(step),
                "settings": {
                    key: value
                    for key, value in {
                        "endpoint": step.get("endpoint") or "",
                        "database": step.get("database") or "",
                        "advanced": step.get("content") or "",
                    }.items()
                    if value
                },
                "runtime": f"nodes/{node_dir}",
            }
        nodes.append(node_entry)

    engines = {
        "dagster": (
            {
                "mode": "local-primary",
                "compose": "docker-compose.yml",
                "project": "dagster",
            }
            if targets.get("dagster")
            else None
        ),
        "argo": (
            {
                "mode": "kubernetes-export",
                "workflow": "argo/workflow.yaml",
                "image_strategy": "shared",
                "image": (shared_argo_runtime or {}).get("image"),
                "dockerfile": (shared_argo_runtime or {}).get("dockerfile"),
                "environment_hash": (shared_argo_runtime or {}).get(
                    "environment_hash"
                ),
            }
            if targets.get("argo")
            else None
        ),
    }
    return {
        "schema_version": "inlumen.run-spec@3",
        "artifact_contract": dict(ARTIFACT_CONTRACT),
        "runtime": {
            "default_engine": "dagster" if targets.get("dagster") else "argo",
            "package_manager": "uv",
            "python": "3.11",
        },
        "node_order": list(ordered_ids),
        "nodes": nodes,
        "connections": resolved_connections,
        "run_inputs": {
            "path": "inputs",
            "layout": "inputs/<source-node>/...",
            "lifecycle": "source-owned",
            "transport": "filesystem",
            "fixture_policy": "files are attached to Source nodes",
        },
        "outputs": {
            "path": "outputs",
            "layout": "outputs/<node>/<run-id>/<output-port>/...",
            "transport": "filesystem",
        },
        "engines": engines,
    }


def _dedupe_bundle_files(files: Sequence[dict]) -> List[dict]:
    by_path: dict[str, dict] = {}
    for file_entry in files:
        path = _clean_string(file_entry.get("path"))
        if not path:
            continue
        by_path[path] = file_entry
    return [by_path[path] for path in sorted(by_path)]


def build_deployment_bundle_files(
    pipeline_graph: Optional[dict],
    dockerfiles_payload: Any,
    *,
    targets: Optional[dict] = None,
    files: Any = None,
) -> dict:
    selected_targets = {
        "argo": bool((targets or {}).get("argo", True)),
        "dagster": bool((targets or {}).get("dagster", False)),
    }
    if not selected_targets["argo"] and not selected_targets["dagster"]:
        raise ValueError("Select at least one deployment target.")

    all_steps = extract_pipeline_steps(pipeline_graph, files)
    if not all_steps:
        raise ValueError("No pipeline steps were found for deployment bundle generation.")

    edges = extract_pipeline_edges(pipeline_graph)
    steps = select_runtime_steps(all_steps)
    _validate_flat_task_output_contract(steps)
    step_ids = [step["flow_id"] for step in steps]
    dockerfiles = _dockerfiles_from_payload(dockerfiles_payload)
    if not dockerfiles:
        raise ValueError("Dockerfile metadata is required for deployment bundle generation.")
    validate_dockerfile_artifacts(dockerfiles, step_ids, steps)

    explicit_edges = [
        edge for edge in edges if edge.get("source") in step_ids and edge.get("target") in step_ids
    ]
    if not explicit_edges:
        explicit_edges = [
            {"source": step_ids[idx], "target": step_ids[idx + 1]}
            for idx in range(len(step_ids) - 1)
        ]
    ordered_ids = _topological_order(step_ids, explicit_edges)
    dependencies = _dependency_lookup(step_ids, explicit_edges)
    steps_by_id = {step["flow_id"]: step for step in steps}
    resolved_bindings = _resolved_artifact_bindings(steps_by_id, explicit_edges)
    resolved_connections = [
        {
            "source": binding.source_node,
            "source_port": binding.source_port,
            "target": binding.target_node,
            "target_port": binding.target_port,
        }
        for binding in resolved_bindings
    ]
    secret_environment_names = sorted({
        runtime_secret_name(step["flow_id"], key)
        for step in steps
        for key in _runtime_secret_parameters(step, dockerfiles_payload)
    })
    runtime_environment_by_step = {
        step_id: _runtime_environment_for_step(dockerfiles_payload, step_id)
        for step_id in ordered_ids
    }
    runtime_environment = [
        {**requirement, "flow_id": step_id}
        for step_id in ordered_ids
        for requirement in runtime_environment_by_step[step_id]
    ]

    bundle_files: List[dict] = []
    node_entries = []
    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        node_dir = _bundle_node_dir(step)
        node_entries.append(
            {
                "flow_id": step_id,
                "label": step.get("label") or "",
                "type": step.get("type") or "task",
                "template": step.get("template") or "",
                "ports": step.get("ports") or normalize_node_ports(
                    None,
                    step.get("type") or "task",
                ),
                "implementation": step.get("implementation") or {},
                "runtime_environment": runtime_environment_by_step[step_id],
                "path": f"nodes/{node_dir}",
                "output_path": f"outputs/{node_dir}",
                "parents": dependencies.get(step_id) or [],
                "input_bindings": [
                    connection
                    for connection in resolved_connections
                    if connection["target"] == step_id
                ],
            }
        )
        for file_entry in _deployment_files_for_step(dockerfiles_payload, step_id):
            filename = _clean_string(file_entry.get("filename"))
            if not filename:
                continue
            bundle_files.append(
                _bundle_file(
                    f"nodes/{node_dir}/{_safe_docker_source(filename)}",
                    str(file_entry.get("content") or ""),
                    role=str(file_entry.get("role") or "runtime"),
                    flow_id=step_id,
                    content_type=str(file_entry.get("content_type") or "text/plain;charset=utf-8"),
                    content_encoding=_clean_string(
                        file_entry.get("content_encoding")
                    ),
                    size_bytes=file_entry.get("size_bytes"),
                    sha256=_clean_string(file_entry.get("sha256")),
                )
            )
        bundle_files.append(
            _bundle_file(
                f"outputs/{node_dir}/.gitkeep",
                "",
                role="output-placeholder",
                flow_id=step_id,
                content_type="text/plain",
            )
        )

    root_input_files = _root_input_files_for_dagster(steps, dependencies, dockerfiles_payload)
    input_errors = _required_root_input_errors(
        steps,
        dependencies,
        dockerfiles_payload,
        root_input_files,
    )
    if input_errors:
        raise DeploymentArtifactValidationError(
            "Deployment bundle input validation failed",
            input_errors,
        )
    _validate_explicit_input_integrity(dockerfiles_payload, root_input_files)
    root_step_ids = [
        step_id for step_id in ordered_ids if not dependencies.get(step_id)
    ]
    for file_entry in root_input_files:
        owner = _root_input_owner(file_entry, root_step_ids)
        filename = _safe_docker_source(_clean_string(file_entry.get("filename")))
        bundle_files.append(
            _bundle_file(
                f"inputs/{_bundle_node_dir(steps_by_id[owner])}/{filename}",
                str(file_entry.get("content") or ""),
                role="input",
                flow_id=_clean_string(file_entry.get("flow_id")),
                content_type=str(file_entry.get("content_type") or "text/plain;charset=utf-8"),
                content_encoding=_clean_string(
                    file_entry.get("content_encoding")
                ),
                size_bytes=file_entry.get("size_bytes"),
                sha256=_clean_string(file_entry.get("sha256")),
            )
        )
    if not root_input_files:
        # Runtime-backed Sources (for example Database) do not have uploaded
        # fixture files, but the bundle still needs an inputs/ mount point.
        bundle_files.append(
            _bundle_file(
                "inputs/.gitkeep",
                "",
                role="input-directory-placeholder",
                content_type="text/plain",
            )
        )
    argo_workflow_path = None
    shared_argo_runtime = None
    if selected_targets["argo"]:
        shared_argo_runtime = _shared_argo_runtime(
            steps,
            ordered_ids,
            dockerfiles_payload,
        )
        argo_workflow_path = "argo/workflow.yaml"
        bundle_files.extend(
            [
                _bundle_file(
                    argo_workflow_path,
                    build_argo_workflow_yaml(
                        pipeline_graph,
                        dockerfiles_payload,
                        files,
                        shared_runtime=shared_argo_runtime,
                    ),
                    role="argo-workflow",
                    content_type="application/x-yaml;charset=utf-8",
                ),
                _bundle_file(
                    "argo/Dockerfile",
                    shared_argo_runtime["dockerfile_content"],
                    role="argo-runtime",
                    content_type="text/x-dockerfile",
                ),
                _bundle_file(
                    "argo/requirements.txt",
                    shared_argo_runtime["requirements_content"],
                    role="argo-runtime",
                ),
            ]
        )

    dagster_project_path = None
    model_requirements = _model_requirements_for_dagster(
        [steps_by_id[step_id] for step_id in ordered_ids],
        dockerfiles_payload,
    )
    has_model_requirements = bool(model_requirements["models"])
    if selected_targets["dagster"]:
        dagster_project_path = "dagster"
        bundle_files.extend(
            build_dagster_project_files(
                pipeline_graph,
                dockerfiles_payload,
                files,
                project_dir=dagster_project_path,
                bundle_layout=True,
            )
        )
        bundle_files.append(
            _bundle_file(
                "docker-compose.yml",
                _bundle_root_docker_compose_content(
                    has_model_requirements=has_model_requirements,
                ),
                role="dagster-compose",
                content_type="application/x-yaml;charset=utf-8",
            )
        )

    bundle_files.append(
        _bundle_file(
            "README.md",
            _deployment_bundle_readme_content(
                targets=selected_targets,
                has_model_requirements=has_model_requirements,
                secret_environment_names=secret_environment_names,
                runtime_environment=runtime_environment,
            ),
            role="bundle-readme",
            content_type="text/markdown;charset=utf-8",
        )
    )

    env_example_names = sorted(
        set(secret_environment_names)
        | {item["name"] for item in runtime_environment}
    )
    if env_example_names:
        bundle_files.append(
            _bundle_file(
                ".env.example",
                "\n".join(f"{name}=" for name in env_example_names) + "\n",
                role="secret-environment-example",
                content_type="text/plain;charset=utf-8",
            )
        )

    run_spec = build_run_spec(
        steps=steps,
        ordered_ids=ordered_ids,
        connections=explicit_edges,
        dependencies=dependencies,
        dockerfiles_payload=dockerfiles_payload,
        targets=selected_targets,
        shared_argo_runtime=shared_argo_runtime,
    )
    bundle_files.append(
        _bundle_file(
            "run-spec.json",
            json.dumps(run_spec, indent=2) + "\n",
            role="run-spec",
            content_type="application/json",
        )
    )

    manifest = {
        "schema_version": "inlumen.deployment-bundle@2",
        "artifact_contract": dict(ARTIFACT_CONTRACT),
        "run_spec": "run-spec.json",
        "targets": selected_targets,
        "readme": "README.md",
        "node_order": ordered_ids,
        "nodes": node_entries,
        "connections": resolved_connections,
        "runtime_environment": runtime_environment,
        "inputs": {
            "path": "inputs",
            "file_count": len(root_input_files),
            "lifecycle": "source-owned",
            "transport": "filesystem",
        },
        "sensitive_parameters": [
            reference
            for node in run_spec["nodes"]
            for reference in node.get("secret_parameters", [])
        ],
        "outputs": {
            "path": "outputs",
            "per_node": [entry["output_path"] for entry in node_entries],
        },
        "argo": (
            {
                "workflow": argo_workflow_path,
                "dockerfile": shared_argo_runtime["dockerfile"],
                "requirements": shared_argo_runtime["requirements"],
                "image": shared_argo_runtime["image"],
                "image_strategy": "shared",
                "environment_hash": shared_argo_runtime["environment_hash"],
            }
            if shared_argo_runtime
            else None
        ),
        "dagster": (
            {
                "project": dagster_project_path,
                "dockerfile": "dagster/Dockerfile",
                "compose": "docker-compose.yml",
                "project_compose": "dagster/docker-compose.yml",
                "dagster_version": DAGSTER_PINNED_VERSION,
                "model_artifact_count": len(model_requirements["models"]),
            }
            if dagster_project_path
            else None
        ),
    }
    bundle_files.append(
        _bundle_file(
            "bundle-manifest.json",
            json.dumps(manifest, indent=2) + "\n",
            role="bundle-manifest",
            content_type="application/json",
        )
    )

    deduped = _dedupe_bundle_files(bundle_files)
    return {
        "files": deduped,
        "manifest": manifest,
        "guardrails": {
            "valid": True,
            "checks": [
                "canonical deployment bundle layout generated",
                "engine-neutral inlumen.run-spec@3 generated",
                "node runtime artifacts copied under nodes/<node>/",
                "root inputs and per-node output directories declared",
                "selected deployment targets generated deterministically",
                "Argo reuses one shared pipeline image when selected",
            ],
        },
    }
