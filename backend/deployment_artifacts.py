import ast
import io
import json
import keyword
import re
import zipfile
from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
GENERIC_STEP_NAME_RE = re.compile(r"^step[_\s-]*\d+(?:[_\s-].*)?$", re.IGNORECASE)
GENERIC_STEP_PREFIX_RE = re.compile(r"^step[_\s-]*\d+[_\s-]*", re.IGNORECASE)


class DeploymentArtifactValidationError(ValueError):
    """Raised when generated deployment artifacts fail the format guardrails."""

    def __init__(self, message: str, errors: Sequence[str]):
        self.errors = list(errors)
        detail = "; ".join(self.errors)
        super().__init__(f"{message}: {detail}" if detail else message)


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _resolved_step_name(data: dict, flow_id: str, files: Sequence[dict]) -> str:
    candidates = [
        data.get("name"),
        data.get("title"),
        data.get("step_name"),
        data.get("display_name"),
        data.get("label"),
    ]
    for candidate in candidates:
        value = _clean_string(candidate)
        if value and not GENERIC_STEP_NAME_RE.fullmatch(value):
            return value

    for file_ref in files:
        filename = PurePosixPath(_clean_string(file_ref.get("filename"))).name
        if filename and PurePosixPath(filename).suffix.lower() == ".py":
            stem = PurePosixPath(filename).stem
            meaningful_stem = GENERIC_STEP_PREFIX_RE.sub("", stem).strip("_- ")
            if meaningful_stem:
                return meaningful_stem.replace("_", " ").replace("-", " ")

    description = _clean_string(data.get("description"))
    if description:
        return description.rstrip(".")

    for candidate in candidates:
        value = _clean_string(candidate)
        if value:
            return value
    return flow_id


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
            }
        )

    if file_refs:
        return file_refs

    for filename in data.get("files") or []:
        if not isinstance(filename, str) or not filename.strip():
            continue
        file_refs.append(
            {
                "filename": filename.strip(),
                "bucket": f"files-step-id-{flow_id}",
                "step_id": flow_id,
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
        files_for_step = row.get("files") or []
        normalized_step_files = normalize_file_refs(
            [
                {
                    "filename": f.get("filename"),
                    "bucket": f.get("bucket") or f"files-step-id-{flow_id}",
                    "step_id": flow_id,
                }
                for f in files_for_step
                if isinstance(f, dict) and f.get("filename")
            ]
        )
        steps_by_id[flow_id] = {
            "flow_id": flow_id,
            "label": _clean_string(step_data.get("label")),
            "name": _resolved_step_name(step_data, flow_id, normalized_step_files),
            "description": _clean_string(step_data.get("description")),
            "type": _clean_string(step_data.get("type")) or "custom",
            "content": _clean_string(step_data.get("content")),
            "endpoint": _clean_string(step_data.get("endpoint")),
            "database": _clean_string(step_data.get("database")),
            "param": step_data.get("param") if isinstance(step_data.get("param"), dict) else {},
            "files": normalized_step_files,
        }

    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else node
        flow_id = _clean_string(data.get("flow_id") or node.get("id") or data.get("id"))
        if not flow_id:
            continue

        param = data.get("param") if isinstance(data.get("param"), dict) else {}
        if not param and isinstance(data.get("param_json"), str):
            try:
                parsed_param = json.loads(data.get("param_json") or "{}")
                param = parsed_param if isinstance(parsed_param, dict) else {}
            except Exception:
                param = {}

        normalized_step_files = _files_from_step_data(data, flow_id)
        steps_by_id[flow_id] = {
            "flow_id": flow_id,
            "label": _clean_string(data.get("label")),
            "name": _resolved_step_name(data, flow_id, normalized_step_files),
            "description": _clean_string(data.get("description")),
            "type": _clean_string(data.get("type")) or "custom",
            "content": _clean_string(data.get("content")),
            "endpoint": _clean_string(data.get("endpoint")),
            "database": _clean_string(data.get("database")),
            "param": param,
            "files": normalized_step_files,
        }

    for step_id, step_files in files_by_step.items():
        if step_id not in steps_by_id:
            steps_by_id[step_id] = {
                "flow_id": step_id,
                "label": "",
                "name": step_id,
                "description": "",
                "type": "custom",
                "content": "",
                "endpoint": "",
                "database": "",
                "param": {},
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
                edges.append({"source": source, "target": target})

    raw_flows = graph.get("flows")
    if isinstance(raw_flows, list):
        for flow in raw_flows:
            if not isinstance(flow, dict):
                continue
            source = _clean_string(flow.get("source"))
            target = _clean_string(flow.get("target"))
            if source and target and source != target:
                edges.append({"source": source, "target": target})

    seen: set[Tuple[str, str]] = set()
    deduped = []
    for edge in edges:
        key = (edge["source"], edge["target"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


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
        lines.append("RUN pip install --no-cache-dir -r requirements.txt")
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

    Runtime Dockerfile generation uses the LLM-backed generator in
    deployment_agents.py so attached files and step semantics can be interpreted
    with natural-language context.
    """
    steps = extract_pipeline_steps(pipeline_graph, files)
    if not steps:
        raise ValueError("No pipeline steps were found for Dockerfile generation.")

    dockerfiles = [_dockerfile_for_step(step) for step in steps]
    validate_dockerfile_artifacts(dockerfiles, [step["flow_id"] for step in steps], steps)
    return {
        "dockerfiles": dockerfiles,
        "guardrails": {
            "valid": True,
            "checks": [
                "one Dockerfile per pipeline step",
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


def _topological_order(
    step_ids: Sequence[str],
    edges: Sequence[dict],
    *,
    error_message: str = "Argo Workflow guardrail validation failed",
    cycle_error: str = "pipeline graph contains a cycle and cannot be represented as an Argo DAG",
) -> List[str]:
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
            error_message,
            [cycle_error],
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


def build_argo_workflow_object(
    pipeline_graph: Optional[dict],
    dockerfiles_payload: Any,
    files: Any = None,
) -> dict:
    steps = extract_pipeline_steps(pipeline_graph, files)
    if not steps:
        raise ValueError("No pipeline steps were found for Argo Workflow generation.")

    step_ids = [step["flow_id"] for step in steps]
    dockerfiles = _dockerfiles_from_payload(dockerfiles_payload)
    if not dockerfiles:
        raise ValueError("Dockerfile metadata is required for Argo Workflow generation.")
    validate_dockerfile_artifacts(dockerfiles, step_ids, steps)

    edges = extract_pipeline_edges(pipeline_graph)
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
    dockerfiles_by_step = _dockerfile_lookup(dockerfiles)

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
        image = _clean_string(dockerfile.get("image")) or f"inlumen/{_argo_name(step_id)}:latest"
        command = dockerfile.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            command = _extract_json_cmd_from_dockerfile(_clean_string(dockerfile.get("content")))
        if not command:
            command = _select_command(step)

        env = [
            {"name": "INLUMEN_FLOW_ID", "value": step_id},
            {"name": "INLUMEN_STEP_TYPE", "value": step.get("type") or "custom"},
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
        for key, value in sorted((step.get("param") or {}).items()):
            env_name = "INLUMEN_PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", str(key)).upper().strip("_")
            if env_name == "INLUMEN_PARAM_":
                continue
            env.append({"name": env_name, "value": str(value)})

        annotations = {
            "inlumen.ai/flow-id": step_id,
            "inlumen.ai/type": step.get("type") or "custom",
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
                    "workingDir": "/app",
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
) -> str:
    steps = extract_pipeline_steps(pipeline_graph, files)
    workflow = build_argo_workflow_object(pipeline_graph, dockerfiles_payload, files)
    yaml_text = dump_yaml(workflow)
    validate_argo_workflow_yaml(yaml_text, [step["flow_id"] for step in steps])
    return yaml_text


def _python_identifier(value: Any, prefix: str = "step") -> str:
    text = _clean_string(value).lower()
    text = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    if not text:
        text = prefix
    if text[0].isdigit():
        text = f"{prefix}_{text}"
    if keyword.iskeyword(text):
        text = f"{text}_{prefix}"
    return text


def _unique_python_identifiers(steps: Sequence[dict]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    used: set[str] = set()
    for step in steps:
        step_id = step["flow_id"]
        base = _python_identifier(step.get("name") or step.get("label") or step_id, "step")
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        names[step_id] = candidate
        used.add(candidate)
    return names


def _dagster_step_metadata(step: dict) -> dict:
    metadata = {
        "flow_id": step.get("flow_id") or "",
        "name": step.get("name") or "",
        "label": step.get("label") or "",
        "description": step.get("description") or "",
        "type": step.get("type") or "custom",
        "endpoint": step.get("endpoint") or "",
        "database": step.get("database") or "",
        "files": [entry["filename"] for entry in step.get("files") or [] if entry.get("filename")],
        "param": step.get("param") or {},
    }
    return {
        key: value
        for key, value in metadata.items()
        if value not in ("", [], {})
    }


def build_dagster_definitions_py(
    pipeline_graph: Optional[dict],
    files: Any = None,
    script_names_by_step: Optional[Dict[str, List[str]]] = None,
    script_invocations_by_step: Optional[Dict[str, List[dict]]] = None,
    scripts_subdirectory: str = "",
    script_runner_filename: str = "",
) -> str:
    steps = extract_pipeline_steps(pipeline_graph, files)
    if not steps:
        raise ValueError("No pipeline steps were found for Dagster definitions generation.")

    step_ids = [step["flow_id"] for step in steps]
    edges = extract_pipeline_edges(pipeline_graph)
    explicit_edges = [
        edge for edge in edges if edge.get("source") in step_ids and edge.get("target") in step_ids
    ]
    if not explicit_edges:
        ordered_ids = [step["flow_id"] for step in steps]
        explicit_edges = [
            {"source": ordered_ids[idx], "target": ordered_ids[idx + 1]}
            for idx in range(len(ordered_ids) - 1)
        ]

    ordered_ids = _topological_order(
        step_ids,
        explicit_edges,
        error_message="Dagster definitions guardrail validation failed",
        cycle_error="pipeline graph contains a cycle and cannot be represented as a Dagster job",
    )
    steps_by_id = {step["flow_id"]: step for step in steps}
    dependencies = _dependency_lookup(step_ids, explicit_edges)
    dagster_names = _unique_python_identifiers(steps)

    lines = [
        '"""Dagster definitions generated by inLUMEN.',
        "",
        "Install dependencies and start Dagster from this directory:",
        "",
        "    python -m pip install -r requirements.txt",
        '"""',
        "",
        "import json",
        "import os",
        "import shutil",
        "import subprocess",
        "import sys",
        "from pathlib import Path",
        "",
        "from dagster import Definitions, in_process_executor, job, op",
        "",
        "",
        "BASE_DIR = Path(__file__).resolve().parent",
        (
            f"SCRIPTS_DIR = BASE_DIR / {json.dumps(scripts_subdirectory)}"
            if scripts_subdirectory
            else "SCRIPTS_DIR = BASE_DIR"
        ),
        "DATA_DIR = Path(os.getenv(\"INLUMEN_DATA_DIR\", BASE_DIR / \"data\"))",
        "OUTPUT_DIR = Path(os.getenv(\"INLUMEN_OUTPUT_DIR\", BASE_DIR / \"output\"))",
        "SOURCE_FILES = {",
        "    \".dockerignore\",",
        "    \"Dockerfile\",",
        "    \"README.md\",",
        "    \"_dagster_script_runner.py\",",
        "    \"definitions.py\",",
        "    \"docker-compose.yml\",",
        "    \"requirements.txt\",",
        "}",
        "",
        "",
        "def project_file_state():",
        "    state = {}",
        "    for path in BASE_DIR.rglob(\"*\"):",
        "        try:",
        "            relative_path = path.relative_to(BASE_DIR)",
        "            if not path.is_file():",
        "                continue",
        "        except OSError:",
        "            continue",
        "        top_level = relative_path.parts[0]",
        "        if top_level in {\"output\", \"scripts\", \".git\", \".dagster\", \"__pycache__\"}:",
        "            continue",
        "        if top_level.startswith(\".tmp_dagster_home_\"):",
        "            continue",
        "        if len(relative_path.parts) == 1 and relative_path.name in SOURCE_FILES:",
        "            continue",
        "        try:",
        "            stat = path.stat()",
        "        except OSError:",
        "            continue",
        "        state[str(relative_path)] = (stat.st_mtime_ns, stat.st_size)",
        "    return state",
        "",
        "",
        "def collect_project_outputs(before_state, step_output_dir):",
        "    produced_files = []",
        "    after_state = project_file_state()",
        "    for relative_name, fingerprint in sorted(after_state.items()):",
        "        if before_state.get(relative_name) == fingerprint:",
        "            continue",
        "        source = BASE_DIR / relative_name",
        "        destination = step_output_dir / \"files\" / relative_name",
        "        destination.parent.mkdir(parents=True, exist_ok=True)",
        "        try:",
        "            shutil.copy2(source, destination)",
        "        except OSError:",
        "            continue",
        "        produced_files.append(relative_name)",
        "    return produced_files",
        "",
        "",
        "def run_step_scripts(context, script_invocations):",
        "    results = []",
        "    step_output_dir = OUTPUT_DIR / context.op.name",
        "    step_output_dir.mkdir(parents=True, exist_ok=True)",
        "    script_env = os.environ.copy()",
        "    script_env[\"INLUMEN_DATA_DIR\"] = str(DATA_DIR)",
        "    script_env[\"INLUMEN_OUTPUT_DIR\"] = str(step_output_dir)",
        "    for invocation in script_invocations:",
        "        if isinstance(invocation, str):",
        "            invocation = {\"script\": invocation, \"interpreter\": \"python\", \"args\": []}",
        "        script_name = invocation[\"script\"]",
        "        interpreter = invocation.get(\"interpreter\", \"python\")",
        "        script_args = [str(value) for value in invocation.get(\"args\", [])]",
        "        script_path = SCRIPTS_DIR / script_name",
        "        if not script_path.is_file():",
        "            raise FileNotFoundError(f\"Step script not found: {script_path}\")",
        "        context.log.info(\"Running %s\", script_path.name)",
        "        command = [sys.executable, str(script_path), *script_args]",
        (
            f"        command = ([sys.executable, str(BASE_DIR / {json.dumps(script_runner_filename)}), str(script_path), *script_args] if interpreter == \"python\" else [interpreter, str(script_path), *script_args])"
            if script_runner_filename
            else "        command = ([sys.executable, str(script_path), *script_args] if interpreter == \"python\" else [interpreter, str(script_path), *script_args])"
        ),
        "        before_state = project_file_state()",
        "        completed = subprocess.run(",
        "            command,",
        "            cwd=BASE_DIR,",
        "            env=script_env,",
        "            check=False,",
        "            capture_output=True,",
        "            text=True,",
        "        )",
        "        if completed.stdout:",
        "            context.log.info(completed.stdout.rstrip())",
        "        if completed.stderr:",
        "            context.log.warning(completed.stderr.rstrip())",
        "        stdout_path = step_output_dir / f\"{script_path.stem}.stdout.log\"",
        "        stderr_path = step_output_dir / f\"{script_path.stem}.stderr.log\"",
        "        stdout_path.write_text(completed.stdout, encoding=\"utf-8\")",
        "        stderr_path.write_text(completed.stderr, encoding=\"utf-8\")",
        "        produced_files = collect_project_outputs(before_state, step_output_dir)",
        "        legacy_output_dir = DATA_DIR / \"output\"",
        "        if legacy_output_dir.is_dir():",
        "            for produced_path in legacy_output_dir.iterdir():",
        "                destination = step_output_dir / produced_path.name",
        "                if produced_path.is_dir():",
        "                    shutil.copytree(produced_path, destination, dirs_exist_ok=True)",
        "                else:",
        "                    shutil.copy2(produced_path, destination)",
        "        manifest_path = step_output_dir / f\"{script_path.stem}.outputs.json\"",
        "        persisted_files = sorted(",
        "            str(path.relative_to(step_output_dir))",
        "            for path in step_output_dir.rglob(\"*\")",
        "            if path.is_file() and path != manifest_path",
        "        )",
        "        manifest_path.write_text(",
        "            json.dumps({",
        "                \"script\": script_path.name,",
        "                \"returncode\": completed.returncode,",
        "                \"files\": persisted_files,",
        "                \"project_files\": produced_files,",
        "            }, indent=2, sort_keys=True) + \"\\n\",",
        "            encoding=\"utf-8\",",
        "        )",
        "        results.append({",
        "            \"script\": script_path.name,",
        "            \"returncode\": completed.returncode,",
        "            \"stdout\": completed.stdout,",
        "            \"output_dir\": str(step_output_dir),",
        "            \"files\": persisted_files,",
        "        })",
        "        if completed.returncode != 0:",
        "            raise subprocess.CalledProcessError(",
        "                completed.returncode, command, completed.stdout, completed.stderr",
        "            )",
        "    return results",
        "",
    ]

    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        op_name = dagster_names[step_id]
        upstream_params = [
            f"upstream_{dagster_names[dependency]}"
            for dependency in dependencies.get(step_id, [])
        ]
        signature = ", ".join(["context", *upstream_params])
        tags = {
            "inlumen/flow_id": step_id,
            "inlumen/type": step.get("type") or "custom",
        }
        if step.get("name"):
            tags["inlumen/name"] = step["name"]
        if step.get("label"):
            tags["inlumen/label"] = step["label"]
        metadata_json = json.dumps(_dagster_step_metadata(step), sort_keys=True)
        if script_invocations_by_step is not None:
            script_invocations = script_invocations_by_step.get(step_id, [])
        else:
            python_scripts = (
                script_names_by_step.get(step_id, [])
                if script_names_by_step is not None
                else [
                    PurePosixPath(entry["filename"]).name
                    for entry in step.get("files") or []
                    if entry.get("filename")
                    and PurePosixPath(entry["filename"]).suffix.lower() == ".py"
                    and PurePosixPath(entry["filename"]).name != "definitions.py"
                ]
            )
            script_invocations = [
                {"script": script_name, "interpreter": "python", "args": []}
                for script_name in python_scripts
            ]

        lines.extend([
            f"@op(name={json.dumps(op_name)}, tags={json.dumps(tags, sort_keys=True)})",
            f"def {op_name}({signature}):",
        ])
        if upstream_params:
            lines.append(f"    _upstream_results = [{', '.join(upstream_params)}]")
        else:
            lines.append("    _upstream_results = []")
        lines.extend([
            f"    metadata = json.loads({json.dumps(metadata_json)})",
            "    context.log.info(",
            "        \"Running inLUMEN step %s (%s) after %d upstream step(s)\",",
            "        metadata.get(\"flow_id\"),",
            "        metadata.get(\"name\", metadata.get(\"label\", metadata.get(\"flow_id\"))),",
            "        len(_upstream_results),",
            "    )",
            f"    script_invocations = {json.dumps(script_invocations)}",
            "    script_results = run_step_scripts(context, script_invocations)",
            "    return {\"metadata\": metadata, \"scripts\": script_results}",
            "",
            "",
        ])

    lines.extend([
        "@job(name=\"inlumen_pipeline\", executor_def=in_process_executor)",
        "def inlumen_pipeline():",
    ])
    for step_id in ordered_ids:
        op_name = dagster_names[step_id]
        dependency_args = ", ".join(
            f"upstream_{dagster_names[dependency]}={dagster_names[dependency]}_result"
            for dependency in dependencies.get(step_id, [])
        )
        lines.append(f"    {op_name}_result = {op_name}({dependency_args})")

    lines.extend([
        "",
        "",
        "defs = Definitions(jobs=[inlumen_pipeline])",
        "",
    ])

    python_text = "\n".join(lines)
    validate_dagster_definitions_py(python_text, step_ids)
    return python_text


def build_dagster_bundle_zip(
    pipeline_graph: Optional[dict],
    attached_files: Sequence[dict],
    packaging_manifest: Optional[dict] = None,
) -> bytes:
    """Build a runnable Dagster project containing definitions, scripts, and input data."""
    steps = extract_pipeline_steps(pipeline_graph)
    if not steps:
        raise ValueError("No pipeline steps were found for Dagster bundle generation.")

    step_names = _unique_python_identifiers(steps)
    scripts_by_step: Dict[str, List[str]] = defaultdict(list)
    script_invocations_by_step: Dict[str, List[dict]] = defaultdict(list)
    archive_files: Dict[str, bytes] = {}
    used_script_names: set[str] = set()
    requirements_parts: List[str] = []
    input_filename_counts: Dict[str, int] = defaultdict(int)
    attached_lookup = {
        (
            _clean_string(entry.get("step_id")),
            PurePosixPath(_clean_string(entry.get("filename"))).name,
        ): entry
        for entry in attached_files
        if isinstance(entry, dict)
    }
    manifest_files = (
        packaging_manifest.get("files")
        if isinstance(packaging_manifest, dict)
        and isinstance(packaging_manifest.get("files"), list)
        else None
    )
    source_entries = manifest_files if manifest_files is not None else attached_files

    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        filename = PurePosixPath(
            _clean_string(entry.get("source_filename") or entry.get("filename"))
        ).name
        suffix = PurePosixPath(filename).suffix.lower()
        role = _clean_string(entry.get("role")).lower()
        if (
            filename
            and role not in {"script", "requirements"}
            and filename.lower() != "requirements.txt"
            and suffix != ".py"
        ):
            input_filename_counts[filename] += 1

    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        step_id = _clean_string(entry.get("step_id"))
        filename = PurePosixPath(
            _clean_string(entry.get("source_filename") or entry.get("filename"))
        ).name
        source_entry = attached_lookup.get((step_id, filename), entry)
        content = source_entry.get("content") if isinstance(source_entry, dict) else None
        if not step_id or not filename or not isinstance(content, (bytes, bytearray)):
            continue

        suffix = PurePosixPath(filename).suffix.lower()
        role = _clean_string(entry.get("role")).lower()
        destination = _clean_string(entry.get("destination"))
        if destination:
            destination = _safe_docker_source(destination)
        if role == "requirements" or filename.lower() == "requirements.txt":
            requirements_parts.append(bytes(content).decode("utf-8", errors="replace"))
        elif role == "script" or (not role and suffix == ".py" and filename != "definitions.py"):
            archive_name = destination or filename
            if archive_name.startswith("scripts/"):
                archive_name = archive_name[len("scripts/"):]
            if archive_name in used_script_names:
                archive_name = f"{step_names.get(step_id, 'step')}_{PurePosixPath(archive_name).name}"
            used_script_names.add(archive_name)
            scripts_by_step[step_id].append(archive_name)
            script_invocations_by_step[step_id].append({
                "script": archive_name,
                "interpreter": _clean_string(entry.get("interpreter")) or (
                    "bash" if suffix == ".sh" else "python"
                ),
                "args": [
                    str(argument)
                    for argument in entry.get("args", [])
                    if isinstance(argument, (str, int, float))
                ],
            })
            archive_files[f"scripts/{archive_name}"] = bytes(content)
        else:
            step_folder = step_names.get(step_id, _python_identifier(step_id))
            archive_path = destination or f"data/{step_folder}/{filename}"
            archive_files[archive_path] = bytes(content)
            if manifest_files is None and input_filename_counts[filename] == 1:
                archive_files[f"data/{filename}"] = bytes(content)

    requirements = "\n".join(part.rstrip() for part in requirements_parts if part.strip()).strip()
    requirement_lines = {
        match.group(1).lower()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for match in [re.match(r"\s*([A-Za-z0-9_.-]+)", line)]
        if match
    }
    if "dagster" not in requirement_lines:
        requirements = f"{requirements}\ndagster".strip()
    if "dagster-webserver" not in requirement_lines:
        requirements = f"{requirements}\ndagster-webserver".strip()

    definitions = build_dagster_definitions_py(
        pipeline_graph,
        script_names_by_step=scripts_by_step,
        script_invocations_by_step=script_invocations_by_step,
        scripts_subdirectory="scripts",
        script_runner_filename="_dagster_script_runner.py",
    )
    script_runner = """import runpy
import sys


def main():
    script_path = sys.argv[1]
    script_args = sys.argv[2:]
    sys.argv = [script_path, *script_args]
    namespace = runpy.run_path(script_path, run_name="__dagster_script__")
    entrypoint = namespace.get("run") or namespace.get("main")
    if callable(entrypoint):
        entrypoint()


if __name__ == "__main__":
    main()
"""
    dockerfile = """FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    DAGSTER_HOME=/app/.dagster \
    INLUMEN_DATA_DIR=/app/data \
    INLUMEN_OUTPUT_DIR=/app/output

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY definitions.py .
COPY _dagster_script_runner.py .
COPY scripts/ ./scripts/
COPY data/ ./data/

RUN mkdir -p /app/.dagster /app/output

EXPOSE 3000

CMD ["dagster", "dev", "-h", "0.0.0.0", "-p", "3000", "-f", "definitions.py"]
"""
    compose = """name: inlumen-dagster

volumes:
  dagster_home:

services:
  dagster:
    container_name: inlumen-dagster
    build:
      context: .
      dockerfile: Dockerfile
    working_dir: /app
    ports:
      - "3000:3000"
    environment:
      DAGSTER_HOME: /app/.dagster
      INLUMEN_DATA_DIR: /app/data
      INLUMEN_OUTPUT_DIR: /app/output
    volumes:
      - .:/app
      - dagster_home:/app/.dagster
    restart: unless-stopped
"""
    dockerignore = """__pycache__/
*.py[cod]
.DS_Store
output/*
!output/.gitkeep
"""
    readme = """# inLUMEN Dagster project

## Run with Docker Compose

```bash
docker compose up --build
```

Open http://localhost:3000.

The complete extracted project directory is mounted at `/app`, and the
container is named `inlumen-dagster`. Dagster's SQLite metadata is stored in
the Docker-managed `dagster_home` volume to avoid database corruption or
`SIGBUS` crashes on host bind mounts. Python step files are in `scripts/`.
All input files are under `data/`, grouped by step where needed.

Each step receives its own directory under `output/`. Standard output,
standard error, an output manifest, files written through
`INLUMEN_OUTPUT_DIR`, and new or modified project/data files are persisted
there. Because the complete project is mounted, any other files created by a
script also remain visible on the host.

Dagster run history is stored under `.dagster/`. Empty `*.stdout.log` and
`*.stderr.log` files are normal when a script does not print anything or write
diagnostics to standard error.

When present, `packaging_manifest.json` records the validated LLM decisions
for file placement and script arguments. The LLM does not generate or modify
the supplied scripts.

The generated runner automatically calls a script's `run()` or `main()`
function. Scripts do not need an `if __name__ == "__main__"` block.

Scripts can use these environment variables:

- `INLUMEN_DATA_DIR`: input data root (`/app/data` in Docker)
- `INLUMEN_OUTPUT_DIR`: writable output root (`/app/output` in Docker)

## Run without Docker

```bash
python -m pip install -r requirements.txt
dagster dev -f definitions.py
```
"""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("definitions.py", definitions)
        archive.writestr("_dagster_script_runner.py", script_runner)
        archive.writestr("requirements.txt", requirements + "\n")
        archive.writestr("README.md", readme)
        archive.writestr("Dockerfile", dockerfile)
        archive.writestr("docker-compose.yml", compose)
        archive.writestr(".dockerignore", dockerignore)
        if packaging_manifest is not None:
            archive.writestr(
                "packaging_manifest.json",
                json.dumps(packaging_manifest, indent=2, sort_keys=True) + "\n",
            )
        archive.writestr(".dagster/", b"")
        archive.writestr(".dagster/.gitkeep", b"")
        archive.writestr("scripts/", b"")
        archive.writestr("data/", b"")
        archive.writestr("output/", b"")
        archive.writestr("output/.gitkeep", b"")
        for archive_name, content in sorted(archive_files.items()):
            archive.writestr(archive_name, content)
    return output.getvalue()


def validate_dagster_definitions_py(
    python_text: str,
    expected_step_ids: Optional[Iterable[str]] = None,
) -> None:
    errors: List[str] = []
    if not _clean_string(python_text):
        errors.append("Python content is empty")
    if "```" in python_text:
        errors.append("Python content contains markdown code fences")
    try:
        tree = ast.parse(python_text or "")
    except SyntaxError as exc:
        errors.append(f"Python parsing failed: {exc}")
        tree = None

    if tree is not None:
        imports_dagster = any(
            isinstance(node, ast.ImportFrom) and node.module == "dagster"
            for node in tree.body
        )
        if not imports_dagster:
            errors.append("Python content must import from dagster")

        op_count = 0
        job_count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                func = call.func if call is not None else decorator
                if isinstance(func, ast.Name) and func.id == "op":
                    op_count += 1
                if isinstance(func, ast.Name) and func.id == "job":
                    job_count += 1
        if job_count != 1:
            errors.append("Python content must define exactly one Dagster job")

        expected_ids = [_clean_string(step_id) for step_id in (expected_step_ids or []) if _clean_string(step_id)]
        if expected_ids and op_count != len(expected_ids):
            errors.append(f"expected {len(expected_ids)} Dagster ops but found {op_count}")
        for step_id in expected_ids:
            if step_id not in python_text:
                errors.append(f"missing Dagster metadata for step id '{step_id}'")

        has_defs = any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "defs" for target in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "Definitions"
            for node in tree.body
        )
        if not has_defs:
            errors.append("Python content must assign Dagster Definitions to defs")

    if errors:
        raise DeploymentArtifactValidationError("Dagster definitions guardrail validation failed", errors)
