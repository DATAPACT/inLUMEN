import json
import re
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
CODEGEN_GENERATOR = "inlumen-codegen-service"


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
        steps_by_id[flow_id] = {
            "flow_id": flow_id,
            "label": _clean_string(step_data.get("label")),
            "description": _clean_string(step_data.get("description")),
            "type": _clean_string(step_data.get("type")) or "custom",
            "content": _clean_string(step_data.get("content")),
            "endpoint": _clean_string(step_data.get("endpoint")),
            "database": _clean_string(step_data.get("database")),
            "param": step_data.get("param") if isinstance(step_data.get("param"), dict) else {},
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
            "type": _clean_string(data.get("type")) or "custom",
            "content": _clean_string(data.get("content")),
            "endpoint": _clean_string(data.get("endpoint")),
            "database": _clean_string(data.get("database")),
            "param": param,
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
                "type": "custom",
                "content": "",
                "endpoint": "",
                "database": "",
                "param": {},
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


def select_runtime_steps(
    steps: Sequence[dict],
) -> List[dict]:
    return list(steps)


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
    if _clean_string(artifact.get("generator")) != CODEGEN_GENERATOR:
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
        if _clean_string(dockerfile.get("generator")) != CODEGEN_GENERATOR:
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
    ordered_ids: Sequence[str],
    dependencies: Dict[str, List[str]],
) -> None:
    errors: List[str] = []
    for step_id in ordered_ids:
        parents = dependencies.get(step_id) or []
        if len(parents) > 1:
            errors.append(
                f"Node {step_id} has multiple upstream parents; generated-script Argo "
                "handoff currently requires an explicit merge node first."
            )
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
    dockerfiles_by_step: Dict[str, dict],
) -> dict:
    _validate_codegen_argo_shape(
        ordered_ids=ordered_ids,
        dependencies=dependencies,
    )

    steps_by_id = {step["flow_id"]: step for step in steps}
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
    if leaf_ids:
        entry_template["outputs"] = {
            "artifacts": [
                {
                    "name": "result" if len(leaf_ids) == 1 else f"result-{_argo_name(leaf_id)}",
                    "from": f"{{{{tasks.{_argo_name(leaf_id)}.outputs.artifacts.outputs}}}}",
                }
                for leaf_id in leaf_ids
            ]
        }

    templates = [entry_template]
    image_parameters = []

    for step_id in ordered_ids:
        parent_ids = dependencies.get(step_id) or []
        task = {
            "name": _argo_name(step_id),
            "template": _argo_name(step_id),
            "arguments": {
                "artifacts": [
                    {
                        "name": "inputs",
                        **(
                            {
                                "from": f"{{{{tasks.{_argo_name(parent_ids[0])}.outputs.artifacts.outputs}}}}"
                            }
                            if parent_ids
                            else {
                                "s3": {
                                    "key": "{{workflow.parameters.input-artifact-key}}"
                                }
                            }
                        ),
                    }
                ]
            },
        }
        if parent_ids:
            task["dependencies"] = [_argo_name(parent) for parent in parent_ids]
        tasks.append(task)

    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        dockerfile = dockerfiles_by_step[step_id]
        contract = _step_data_contract(step)
        image_parameter = _argo_name(f"image-{step_id}", "image")
        image_parameters.append(
            {
                "name": image_parameter,
                "value": dockerfile["image"],
            }
        )

        command = dockerfile.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            command = _extract_json_cmd_from_dockerfile(_clean_string(dockerfile.get("content")))
        if not command:
            command = ["python", "/app/main.py"]

        output_artifact = {
            "name": "outputs",
            "path": "/inlumen/outputs",
            "archive": {"none": {}},
        }
        if step_id in leaf_ids:
            output_artifact["s3"] = {
                "key": f"{{{{workflow.parameters.output-artifact-prefix}}}}/{_argo_name(step_id)}"
            }

        env = [
            {"name": "INLUMEN_FLOW_ID", "value": step_id},
            {
                "name": _contract_env_name(
                    contract,
                    "input_manifest_env",
                    "INLUMEN_INPUT_MANIFEST",
                ),
                "value": "/inlumen/inputs/input_manifest.json",
            },
            {
                "name": _contract_env_name(
                    contract,
                    "output_dir_env",
                    "INLUMEN_OUTPUT_DIR",
                ),
                "value": "/inlumen/outputs",
            },
            {
                "name": _contract_env_name(
                    contract,
                    "output_manifest_env",
                    "INLUMEN_OUTPUT_MANIFEST",
                ),
                "value": "/inlumen/outputs/output_manifest.json",
            },
            {
                "name": _contract_env_name(
                    contract,
                    "context_path_env",
                    "INLUMEN_CONTEXT_PATH",
                ),
                "value": "/app/node-manifest.json",
            },
        ]
        if step.get("label"):
            env.append({"name": "INLUMEN_STEP_LABEL", "value": step["label"]})
        if step.get("description"):
            env.append({"name": "INLUMEN_STEP_DESCRIPTION", "value": step["description"]})

        annotations = {
            "inlumen.ai/flow-id": step_id,
            "inlumen.ai/type": step.get("type") or "custom",
            "inlumen.ai/generator": CODEGEN_GENERATOR,
            "inlumen.ai/dockerfile": dockerfile["dockerfile_filename"],
        }
        if dockerfile.get("configuration_hash"):
            annotations["inlumen.ai/configuration-hash"] = dockerfile["configuration_hash"]
        if step.get("label"):
            annotations["inlumen.ai/label"] = step["label"]

        templates.append(
            {
                "name": _argo_name(step_id),
                "metadata": {"annotations": annotations},
                "inputs": {
                    "artifacts": [
                        {
                            "name": "inputs",
                            "path": "/inlumen/inputs",
                        }
                    ]
                },
                "outputs": {"artifacts": [output_artifact]},
                "container": {
                    "image": f"{{{{workflow.parameters.{image_parameter}}}}}",
                    "imagePullPolicy": "IfNotPresent",
                    "workingDir": "/app",
                    "command": command,
                    "env": env,
                },
            }
        )

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
                    {
                        "name": "input-artifact-key",
                        "value": "inlumen/input/input-artifact.tgz",
                    },
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
) -> dict:
    all_steps = extract_pipeline_steps(pipeline_graph, files)
    if not all_steps:
        raise ValueError("No pipeline steps were found for Argo Workflow generation.")

    edges = extract_pipeline_edges(pipeline_graph)
    steps = select_runtime_steps(all_steps)
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
    dockerfiles_by_step = _dockerfile_lookup(dockerfiles)

    if steps and (
        all(_is_current_codegen_step(step) for step in steps)
        or _is_codegen_dockerfile_payload(step_ids, dockerfiles_by_step)
    ):
        workflow = _build_codegen_argo_workflow_object(
            steps=steps,
            ordered_ids=ordered_ids,
            dependencies=dependencies,
            dockerfiles_by_step=dockerfiles_by_step,
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
    runtime_steps = select_runtime_steps(steps)
    workflow = build_argo_workflow_object(pipeline_graph, dockerfiles_payload, files)
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


def _payload_runtime_artifacts(dockerfiles_payload: Any) -> List[dict]:
    if not isinstance(dockerfiles_payload, dict):
        return []
    artifacts = dockerfiles_payload.get("runtime_artifacts")
    return [item for item in artifacts if isinstance(item, dict)] if isinstance(artifacts, list) else []


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
    ]
    if files:
        return files

    output = []
    for artifact in _payload_runtime_artifacts(dockerfiles_payload):
        if _clean_string(artifact.get("flow_id")) != flow_id:
            continue
        for file_entry in artifact.get("files") or []:
            if isinstance(file_entry, dict):
                output.append(
                    {
                        "filename": file_entry.get("filename"),
                        "flow_id": flow_id,
                        "content": file_entry.get("content") or "",
                        "content_type": file_entry.get("content_type") or "text/plain",
                        "role": "runtime",
                    }
                )
    return output


def _dagster_file(path: str, content: str, role: str = "dagster") -> dict:
    return {
        "path": path,
        "filename": PurePosixPath(path).name,
        "flow_id": "",
        "content": content,
        "content_type": "text/plain;charset=utf-8",
        "role": role,
    }


def _dagster_yaml(data: dict) -> str:
    return dump_yaml(data)


def _dagster_shell_command_component_source() -> str:
    return '''from pathlib import Path

import dagster as dg


class ShellCommand(dg.Component, dg.Model, dg.Resolvable):
    asset_key: str
    script_path: str
    upstream_assets: list[str] = []
    input_manifest_path: str
    output_dir: str
    output_manifest_path: str
    context_path: str = ""
    arguments: list[str] = []

    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        deps = [dg.AssetKey(asset_key) for asset_key in self.upstream_assets]

        @dg.asset(name=self.asset_key, deps=deps)
        def run_script(asset_context: dg.AssetExecutionContext) -> dg.MaterializeResult:
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

            env = {
                "INLUMEN_FLOW_ID": self.asset_key,
                "INLUMEN_INPUT_MANIFEST": str(input_manifest_path),
                "INLUMEN_OUTPUT_DIR": str(output_dir),
                "INLUMEN_OUTPUT_MANIFEST": str(output_manifest_path),
            }
            if self.context_path:
                context_path = Path(self.context_path)
                if not context_path.is_absolute():
                    context_path = project_root / context_path
                env["INLUMEN_CONTEXT_PATH"] = str(context_path)

            result = dg.PipesSubprocessClient().run(
                command=["python", str(script_path), *self.arguments],
                context=asset_context,
                env=env,
                extras={
                    "input_manifest_path": str(input_manifest_path),
                    "output_dir": str(output_dir),
                    "output_manifest_path": str(output_manifest_path),
                },
            )
            try:
                return result.get_materialize_result()
            except Exception:
                return dg.MaterializeResult(
                    metadata={
                        "output_dir": str(output_dir),
                        "output_manifest_path": str(output_manifest_path),
                    }
                )

        return dg.Definitions(assets=[run_script])
'''


def _dagster_readme(
    *,
    asset_names: Sequence[str],
    has_sample_inputs: bool,
) -> str:
    sample_note = (
        "Sample input files from InLumen were copied into `storage/inputs/`."
        if has_sample_inputs
        else "No sample input files were detected; add files beside `storage/inputs/input_manifest.json` before materializing root assets."
    )
    return f"""# InLumen Dagster Deployment

This project was generated deterministically from persisted InLumen runtime artifacts.

## Assets

{chr(10).join(f"- `{name}`" for name in asset_names)}

## Run Locally

```bash
pip install -e .
dagster dev -m inlumen_dagster_project.definitions
```

The reusable component in `src/inlumen_dagster_project/components/shell_command.py` launches each node script with Dagster Pipes and preserves the InLumen runtime contract:

- `INLUMEN_INPUT_MANIFEST`
- `INLUMEN_OUTPUT_DIR`
- `INLUMEN_OUTPUT_MANIFEST`
- `INLUMEN_CONTEXT_PATH`

{sample_note}
"""


def _dagster_project_metadata_content(install_requires: Sequence[str]) -> str:
    dependencies = [
        "dagster>=1.13,<2.0",
        "dagster-pipes>=1.13,<2.0",
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


def _dagster_dockerfile_content() -> str:
    return """FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir --upgrade pip \\
    && pip install --no-cache-dir -e .
EXPOSE 3000
CMD ["dagster", "dev", "-m", "inlumen_dagster_project.definitions", "-h", "0.0.0.0", "-p", "3000"]
"""


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
        for file_entry in _deployment_files_for_step(dockerfiles_payload, flow_id):
            filename = _clean_string(file_entry.get("filename"))
            if not filename:
                continue
            if filename in runtime_filenames or filename.startswith("Dockerfile."):
                continue
            input_files.append(file_entry)
    return input_files


def _input_manifest_for_dagster(input_files: Sequence[dict]) -> str:
    manifest = {
        "files": [
            {
                "filename": _clean_string(file_entry.get("filename")),
                "kind": "file",
                "format": PurePosixPath(_clean_string(file_entry.get("filename"))).suffix.lstrip(".") or "text",
                "description": "Sample input file copied from InLumen.",
            }
            for file_entry in input_files
            if _clean_string(file_entry.get("filename"))
        ]
    }
    return json.dumps(manifest, indent=2) + "\n"


def build_dagster_project_files(
    pipeline_graph: Optional[dict],
    dockerfiles_payload: Any,
    files: Any = None,
) -> List[dict]:
    all_steps = extract_pipeline_steps(pipeline_graph, files)
    if not all_steps:
        raise ValueError("No pipeline steps were found for Dagster project generation.")

    edges = extract_pipeline_edges(pipeline_graph)
    steps = select_runtime_steps(all_steps)
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
    _require_single_parent_handoff(ordered_ids, dependencies)

    steps_by_id = {step["flow_id"]: step for step in steps}
    asset_names = _dagster_asset_names([steps_by_id[step_id] for step_id in ordered_ids])
    output_files: List[dict] = []
    aggregate_requirements: List[str] = []

    root_input_files = _root_input_files_for_dagster(steps, dependencies, dockerfiles_payload)
    for file_entry in root_input_files:
        filename = _safe_docker_source(_clean_string(file_entry.get("filename")))
        output_files.append(
            _dagster_file(
                f"dagster_project/storage/inputs/{filename}",
                str(file_entry.get("content") or ""),
                "dagster-input",
            )
        )
    output_files.append(
        _dagster_file(
            "dagster_project/storage/inputs/input_manifest.json",
            _input_manifest_for_dagster(root_input_files),
            "dagster-input",
        )
    )

    for step_id in ordered_ids:
        step = steps_by_id[step_id]
        asset_name = asset_names[step_id]
        script_content = _deployment_file_content(dockerfiles_payload, step_id, "main.py")
        if not script_content:
            raise DeploymentArtifactValidationError(
                "Dagster deployment guardrail validation failed",
                [f"Node {step_id} is missing main.py in persisted runtime artifacts."],
            )

        requirements_content = _deployment_file_content(
            dockerfiles_payload,
            step_id,
            "requirements.txt",
        )
        aggregate_requirements.extend(requirements_content.splitlines())

        node_artifact_root = f"dagster_project/src/inlumen_dagster_project/artifacts/nodes/{_sanitize_fragment(step_id, 'step')}"
        script_root = f"dagster_project/src/inlumen_dagster_project/scripts/{asset_name}"
        defs_root = f"dagster_project/src/inlumen_dagster_project/defs/{asset_name}"
        output_files.append(_dagster_file(f"{script_root}/main.py", script_content, "dagster-script"))

        for file_entry in _deployment_files_for_step(dockerfiles_payload, step_id):
            filename = _clean_string(file_entry.get("filename"))
            if not filename:
                continue
            output_files.append(
                _dagster_file(
                    f"{node_artifact_root}/{_safe_docker_source(filename)}",
                    str(file_entry.get("content") or ""),
                    "dagster-node-artifact",
                )
            )

        parents = dependencies.get(step_id) or []
        parent_asset = asset_names[parents[0]] if parents else ""
        input_manifest_path = (
            f"storage/{parent_asset}/output_manifest.json"
            if parent_asset
            else "storage/inputs/input_manifest.json"
        )
        output_dir = f"storage/{asset_name}"
        defs_yaml = _dagster_yaml(
            {
                "type": "inlumen_dagster_project.components.shell_command.ShellCommand",
                "attributes": {
                    "asset_key": asset_name,
                    "script_path": f"src/inlumen_dagster_project/scripts/{asset_name}/main.py",
                    "upstream_assets": [asset_names[parent] for parent in parents],
                    "input_manifest_path": input_manifest_path,
                    "output_dir": output_dir,
                    "output_manifest_path": f"{output_dir}/output_manifest.json",
                    "context_path": (
                        f"src/inlumen_dagster_project/artifacts/nodes/{_sanitize_fragment(step_id, 'step')}/node-manifest.json"
                    ),
                    "arguments": [],
                },
            }
        )
        output_files.append(_dagster_file(f"{defs_root}/defs.yaml", defs_yaml, "dagster-defs"))

    asset_name_list = [asset_names[step_id] for step_id in ordered_ids]
    output_files.extend(
        [
            _dagster_file(
                "dagster_project/pyproject.toml",
                _dagster_project_metadata_content(aggregate_requirements),
                "dagster-project",
            ),
            _dagster_file(
                "dagster_project/Dockerfile",
                _dagster_dockerfile_content(),
                "dagster-project",
            ),
            _dagster_file(
                "dagster_project/README.md",
                _dagster_readme(
                    asset_names=asset_name_list,
                    has_sample_inputs=bool(root_input_files),
                ),
                "dagster-project",
            ),
            _dagster_file(
                "dagster_project/src/inlumen_dagster_project/__init__.py",
                "",
                "dagster-project",
            ),
            _dagster_file(
                "dagster_project/src/inlumen_dagster_project/definitions.py",
                _dagster_definitions_source(),
                "dagster-project",
            ),
            _dagster_file(
                "dagster_project/src/inlumen_dagster_project/components/__init__.py",
                "from .shell_command import ShellCommand\n",
                "dagster-project",
            ),
            _dagster_file(
                "dagster_project/src/inlumen_dagster_project/components/shell_command.py",
                _dagster_shell_command_component_source(),
                "dagster-project",
            ),
            _dagster_file(
                "dagster_project/deployment-manifest.json",
                json.dumps(
                    {
                        "schema_version": "inlumen.dagster-deployment@1",
                        "asset_order": asset_name_list,
                        "source": "inlumen deployment artifacts",
                    },
                    indent=2,
                )
                + "\n",
                "dagster-project",
            ),
        ]
    )
    return output_files
