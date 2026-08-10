import asyncio
import base64
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, has_request_context, make_response, request

from artifact_content import (
    decode_artifact_content,
    encode_artifact_bytes,
    verify_artifact_integrity,
)
from async_runtime import run_async
from auth_middleware import require_auth
from chat_state import clear_state_from_disk, load_state_from_disk, save_state_to_disk
from deployment_artifacts import (
    DeploymentArtifactValidationError,
    build_argo_workflow_yaml,
    build_dagster_project_files,
    build_deployment_bundle_files,
)
from deployment_agents import (
    generate_argo_yaml_from_graph,
    generate_dockerfiles_with_agent,
)
from graph_client import (
    fetch_pipeline_graph,
    fetch_pipeline_versions,
    save_active_pipeline_version,
    sync_backend_to_canvas_graph,
)
from llm_config import llm_config_from_payload, log_llm_selection
from pipeline_editor_team import build_pipeline_editing_team
from pipeline_graph_validation import (
    validate_pipeline_graph,
    validation_issue_messages,
)
from runtime_config import add_cors_headers

app = Flask(__name__)

DEPLOYMENT_VALIDATION_SERVICE_URL = os.getenv(
    "INLUMEN_DEPLOYMENT_VALIDATION_SERVICE_URL",
    "http://127.0.0.1:8020",
).rstrip("/")
DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS = int(
    os.getenv("INLUMEN_DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS", "1800")
)
DEPLOYMENT_VALIDATION_WORK_DIR = Path(
    os.getenv("INLUMEN_DEPLOYMENT_VALIDATION_WORK_DIR", "state/deployment-validation")
)
DEPLOYMENT_VALIDATION_LOCAL_ROOT = Path(
    os.getenv("INLUMEN_DEPLOYMENT_VALIDATION_LOCAL_ROOT", str(Path.cwd()))
).resolve()
DEPLOYMENT_VALIDATION_REMOTE_ROOT = os.getenv(
    "INLUMEN_DEPLOYMENT_VALIDATION_REMOTE_ROOT",
    str(DEPLOYMENT_VALIDATION_LOCAL_ROOT),
)


@app.after_request
def apply_cors(response):
    return add_cors_headers(response, request.headers.get("Origin"))


def _preflight_response():
    return make_response("", 200)


def _safe_bundle_relative_path(path: str) -> Path:
    relative = Path(str(path or "").strip())
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise ValueError(f"Unsafe bundle file path: {path}")
    return relative


def _validation_remote_path(local_path: Path) -> str:
    resolved = local_path.resolve()
    try:
        relative = resolved.relative_to(DEPLOYMENT_VALIDATION_LOCAL_ROOT)
    except ValueError:
        return str(resolved)
    return str(Path(DEPLOYMENT_VALIDATION_REMOTE_ROOT) / relative)


def _write_validation_bundle(files: list[dict]) -> Path:
    bundle_dir = DEPLOYMENT_VALIDATION_WORK_DIR / f"bundle-{uuid.uuid4().hex}"
    if not bundle_dir.is_absolute():
        bundle_dir = Path.cwd() / bundle_dir
    bundle_dir.mkdir(parents=True, exist_ok=True)

    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue
        relative_path = _safe_bundle_relative_path(str(file_entry.get("path") or ""))
        destination = bundle_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = decode_artifact_content(file_entry)
        verify_artifact_integrity(file_entry, content)
        destination.write_bytes(content)
    return bundle_dir


def _post_deployment_validation_request(
    *,
    bundle_path: Path,
    targets: dict,
    options: dict,
) -> dict:
    mode = str(options.get("mode") or "").strip().lower()
    endpoint = "validate-and-repair" if mode in {"repair", "validate-and-repair"} else "validate"
    payload = {
        "bundle_path": _validation_remote_path(bundle_path),
        "targets": targets,
        "materialize": bool(options.get("materialize", targets.get("dagster"))),
        "reinstall": bool(options.get("reinstall", False)),
        "skip_install": bool(options.get("skip_install", False)),
        "validate_argo": bool(options.get("validate_argo", targets.get("argo"))),
        "validate_dagster": bool(options.get("validate_dagster", targets.get("dagster"))),
        "argo_lint": bool(options.get("argo_lint", False)),
        "argo_dry_run": bool(options.get("argo_dry_run", False)),
        "timeout_seconds": int(options.get("timeout_seconds") or DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS),
    }
    encoded = json.dumps(payload).encode("utf-8")
    http_request = Request(
        f"{DEPLOYMENT_VALIDATION_SERVICE_URL}/{endpoint}",
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(http_request, timeout=DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS) as response:
            response_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Deployment validation service rejected request: {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"Deployment validation service unavailable at {DEPLOYMENT_VALIDATION_SERVICE_URL}: {exc}"
        ) from exc

    parsed = json.loads(response_payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Deployment validation service returned an invalid response")
    return parsed


def _skip_validation_bundle_file(path: Path, bundle_root: Path) -> bool:
    try:
        relative = path.relative_to(bundle_root)
    except ValueError:
        return True
    parts = set(relative.parts)
    if parts & {".inlumen_dagster_validation_venv", ".dagster_home", "__pycache__"}:
        return True
    if path.suffix in {".pyc", ".pyo"}:
        return True
    if relative.parts and relative.parts[0] == "outputs" and path.name != ".gitkeep":
        return True
    return False


def _read_validation_bundle_files(bundle_root: Path) -> list[dict]:
    files: list[dict] = []
    for path in sorted(item for item in bundle_root.rglob("*") if item.is_file()):
        if _skip_validation_bundle_file(path, bundle_root):
            continue
        relative = path.relative_to(bundle_root).as_posix()
        encoded = encode_artifact_bytes(
            path.read_bytes(),
            filename=path.name,
            content_type=(
                "application/json"
                if path.suffix == ".json"
                else "application/x-yaml;charset=utf-8"
                if path.suffix in {".yaml", ".yml"}
                else ""
            ),
        )
        files.append(
            {
                "path": relative,
                "filename": path.name,
                "flow_id": "",
                **encoded,
                "role": "runtime",
                **(
                    {"encoding": "base64"}
                    if encoded.get("content_encoding") == "base64"
                    else {}
                ),
            }
        )
    return files


def _read_run_output_files(bundle_root: Path) -> list[dict]:
    output_root = bundle_root / "outputs"
    if not output_root.is_dir():
        return []
    files: list[dict] = []
    for path in sorted(item for item in output_root.rglob("*") if item.is_file()):
        if path.name == ".gitkeep":
            continue
        relative = path.relative_to(bundle_root).as_posix()
        encoded = encode_artifact_bytes(
            path.read_bytes(),
            filename=path.name,
            content_type="application/json" if path.suffix == ".json" else "",
        )
        files.append(
            {
                "path": relative,
                "filename": path.name,
                "flow_id": "",
                **encoded,
                "role": "run-output",
                **(
                    {"encoding": "base64"}
                    if encoded.get("content_encoding") == "base64"
                    else {}
                ),
            }
        )
    return files


def _dockerfile_inputs(files: list[dict]) -> tuple[list[str], list[str], list[str]]:
    filenames = [file["filename"] for file in files]
    buckets = [file["bucket"] for file in files]
    ids = []
    for file, bucket in zip(files, buckets):
        match = re.search(r"files-step-id-(\d+)", bucket)
        step_id = str(file.get("step_id") or "").strip()
        if match:
            ids.append(match.group(1))
        elif step_id:
            ids.append(step_id)
        else:
            raise ValueError(f"Could not extract step id from bucket '{bucket}'.")
    return filenames, buckets, ids


def _file_refs_from_version_graph(graph: dict) -> list[dict]:
    if not isinstance(graph, dict):
        return []

    refs: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else node
        step_id = str(data.get("flow_id") or node.get("id") or data.get("id") or "").strip()
        if not step_id:
            continue
        default_bucket = f"files-step-id-{step_id}".lower()
        raw_files = data.get("file_buckets") if isinstance(data.get("file_buckets"), list) else data.get("files")
        if not isinstance(raw_files, list):
            continue

        for item in raw_files:
            filename = ""
            bucket = default_bucket
            snapshot_bucket = ""
            snapshot_object = ""
            if isinstance(item, str):
                filename = item.strip()
            elif isinstance(item, dict):
                filename = str(item.get("filename") or item.get("name") or "").strip()
                bucket = str(item.get("bucket") or default_bucket).strip().lower()
                snapshot_bucket = str(item.get("snapshot_bucket") or "").strip().lower()
                snapshot_object = str(item.get("snapshot_object") or "").strip()
            if not filename:
                continue
            key = (step_id, filename, bucket, snapshot_object)
            if key in seen:
                continue
            seen.add(key)
            ref = {
                "step_id": step_id,
                "filename": filename,
                "bucket": bucket,
            }
            if snapshot_bucket and snapshot_object:
                ref["snapshot_bucket"] = snapshot_bucket
                ref["snapshot_object"] = snapshot_object
            refs.append(ref)
    return refs


def _assistant_message_from_result(result) -> str:
    for msg in reversed(result.messages or []):
        if getattr(msg, "source", None) in ("assistant", "assistant_agent") and hasattr(msg, "content"):
            return msg.content
    if result.messages:
        return getattr(result.messages[-1], "content", "")
    return ""


GRAPH_MUTATION_RE = re.compile(
    r"\b(add|build|change|clear|complete|connect|create|delete|design|draw|fix|"
    r"generate|heal|improve|insert|link|make|missing|modify|move|optimize|"
    r"recover|reconnect|refine|remove|repair|replace|restore|update)\b",
    re.IGNORECASE,
)


def _message_expects_graph_change(user_message: str) -> bool:
    return bool(GRAPH_MUTATION_RE.search(user_message or ""))


def _graph_counts(graph: dict | None) -> tuple[int, int]:
    if not isinstance(graph, dict):
        return 0, 0
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    return len(nodes), len(edges)


def _clip_text(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return default


def _node_payload(node: dict) -> dict:
    data = node.get("data") if isinstance(node.get("data"), dict) else node
    position = node.get("position") if isinstance(node.get("position"), dict) else {}
    return {
        "id": str(node.get("id", data.get("id", ""))),
        "type": _clip_text(data.get("type", "")),
        "label": _clip_text(data.get("label", "")),
        "description": _clip_text(data.get("description", "")),
        "content": _clip_text(data.get("content", "")),
        "endpoint": _clip_text(data.get("endpoint", "")),
        "database": _clip_text(data.get("database", "")),
        "x": round(_safe_float(position.get("x", data.get("x", 0))), 2),
        "y": round(_safe_float(position.get("y", data.get("y", 0))), 2),
    }


def _graph_signature(graph: dict | None) -> str:
    if not isinstance(graph, dict):
        return json.dumps({"nodes": [], "edges": []}, sort_keys=True)

    cleaned = _clean_client_graph(graph) or {"nodes": [], "edges": []}
    nodes = cleaned["nodes"]
    edges = cleaned["edges"]
    nodes.sort(key=lambda node: str(node.get("id") or ""))
    edges.sort(key=lambda edge: (
        str(edge.get("source") or ""),
        str(edge.get("target") or ""),
        str(edge.get("source_port") or ""),
        str(edge.get("target_port") or ""),
    ))
    return json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True)


def _json_safe_copy(value: object, depth: int = 0) -> object:
    if depth > 8:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip_text(value, 8000)
    if isinstance(value, list):
        return [_json_safe_copy(item, depth + 1) for item in value[:200]]
    if isinstance(value, dict):
        return {
            _clip_text(key, 120): _json_safe_copy(item, depth + 1)
            for key, item in list(value.items())[:200]
            if str(key or "").strip()
        }
    return _clip_text(value, 1000)


def _clean_client_graph(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None

    cleaned_nodes = []
    seen_node_ids: set[str] = set()
    for raw_node in value.get("nodes") or []:
        if not isinstance(raw_node, dict):
            continue
        payload = _node_payload(raw_node)
        node_id = payload["id"].strip()
        if not node_id or node_id in seen_node_ids:
            continue
        seen_node_ids.add(node_id)

        node_data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else raw_node
        for key in (
            "ports",
            "param",
            "parameters",
            "secret_params",
            "implementation",
            "template",
            "template_label",
            "definition_id",
            "definition_version",
            "configuration_status",
            "generated_artifact",
            "source_config",
            "subpipeline",
            "files",
            "file_buckets",
            "has_files",
        ):
            if key in node_data:
                payload[key] = _json_safe_copy(node_data[key])
        if not str(payload.get("template_label") or "").strip() and isinstance(
            payload.get("template"), str
        ):
            payload["template_label"] = payload["template"]
        cleaned_nodes.append(payload)

    cleaned_edges = []
    seen_edge_keys: set[tuple[str, str, str, str]] = set()
    for raw_edge in value.get("edges") or []:
        if not isinstance(raw_edge, dict):
            continue
        source = str(raw_edge.get("source", "")).strip()
        target = str(raw_edge.get("target", "")).strip()
        source_port = str(
            raw_edge.get("sourceHandle") or raw_edge.get("source_port") or ""
        ).strip()
        target_port = str(
            raw_edge.get("targetHandle") or raw_edge.get("target_port") or ""
        ).strip()
        edge_key = (source, target, source_port, target_port)
        if (
            not source
            or not target
            or source == target
            or source not in seen_node_ids
            or target not in seen_node_ids
            or edge_key in seen_edge_keys
        ):
            continue
        seen_edge_keys.add(edge_key)
        cleaned_edges.append({
            "source": source,
            "target": target,
            "source_port": source_port,
            "target_port": target_port,
        })

    return {
        "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None,
        "settings": _json_safe_copy(value.get("settings")) if isinstance(value.get("settings"), dict) else {},
        "nodes": cleaned_nodes,
        "edges": cleaned_edges,
    }


def _graph_for_agent_context(graph: dict | None) -> dict:
    if not isinstance(graph, dict):
        return {"node_count": 0, "edge_count": 0, "nodes": [], "edges": []}
    cleaned = _clean_client_graph(graph) or graph
    raw_nodes = cleaned.get("nodes") if isinstance(cleaned.get("nodes"), list) else []
    raw_edges = cleaned.get("edges") if isinstance(cleaned.get("edges"), list) else []
    nodes = []
    for node in raw_nodes:
        summary = _node_payload(node)
        template = node.get("template_label") or node.get("template")
        if template:
            summary["template"] = _clip_text(template, 160)
        implementation = node.get("implementation")
        if isinstance(implementation, dict):
            summary["implementation"] = {
                key: _clip_text(implementation.get(key), 240)
                for key in ("kind", "task", "domain", "framework", "model_id")
                if implementation.get(key)
            }
        nodes.append(summary)
    edges = [
        {
            "source": edge.get("source"),
            "target": edge.get("target"),
            "source_port": edge.get("source_port"),
            "target_port": edge.get("target_port"),
        }
        for edge in raw_edges
        if isinstance(edge, dict)
    ]
    return {
        "updated_at": cleaned.get("updated_at"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def _pipeline_metadata_for_agent_context(graph: dict | None) -> dict:
    if not isinstance(graph, dict):
        return {}
    pipeline = graph.get("pipeline") if isinstance(graph.get("pipeline"), dict) else {}
    if not pipeline:
        return {}
    return {
        "uid": _clip_text(pipeline.get("uid", ""), 120),
        "name": _clip_text(pipeline.get("name", "")),
        "label": _clip_text(pipeline.get("label", "")),
        "description": _clip_text(pipeline.get("description", ""), 1200),
        "version": _clip_text(pipeline.get("version", "")),
        "active_version_uid": _clip_text(pipeline.get("active_version_uid", ""), 120),
        "active_version_name": _clip_text(pipeline.get("active_version_name", "")),
        "step_count": pipeline.get("step_count"),
    }


def _build_agent_task(
    user_message: str,
    canvas_graph: dict | None,
    backend_graph: dict | None,
) -> str:
    pipeline_metadata = (
        _pipeline_metadata_for_agent_context(backend_graph)
        or _pipeline_metadata_for_agent_context(canvas_graph)
    )
    return (
        f"{user_message}\n\n"
        "CURRENT PIPELINE METADATA (name, active version, and description):\n"
        f"{json.dumps(pipeline_metadata, ensure_ascii=False)}\n\n"
        "CURRENT VISIBLE CANVAS SNAPSHOT (authoritative UI state):\n"
        f"{json.dumps(_graph_for_agent_context(canvas_graph), ensure_ascii=False)}\n\n"
        "CURRENT BACKEND GRAPH SNAPSHOT (Neo4j state after canvas reconciliation):\n"
        f"{json.dumps(_graph_for_agent_context(backend_graph), ensure_ascii=False)}\n\n"
        "Answer and act from the visible canvas snapshot first. If the user asks for "
        "current status, summarize this snapshot instead of relying on older chat "
        "memory. If a tool is needed, the backend has already been reconciled to this "
        "visible canvas before this turn."
    )


async def _safe_fetch_pipeline_graph() -> tuple[dict | None, str | None]:
    try:
        return await fetch_pipeline_graph(
            authorization=_request_authorization_header(),
        ), None
    except Exception as exc:
        print("[analytics_api.py] Failed to fetch pipeline graph for sync guardrail:", exc)
        return None, str(exc)


def _request_authorization_header() -> str | None:
    if not has_request_context():
        return None
    auth_header = request.headers.get("Authorization", "")
    return auth_header if auth_header else None


def _pipeline_graph_from_payload_or_backend(data: dict) -> dict:
    payload_graph = data.get("pipeline_graph")
    if isinstance(payload_graph, dict):
        return payload_graph
    return run_async(
        fetch_pipeline_graph(
            authorization=_request_authorization_header(),
        )
    )


def _build_graph_sync_guardrail(
    before_graph: dict | None,
    after_graph: dict | None,
    user_message: str,
    fetch_error: str | None = None,
    repaired: bool = False,
) -> dict:
    before_nodes, before_edges = _graph_counts(before_graph)
    after_nodes, after_edges = _graph_counts(after_graph)
    expected_graph_change = _message_expects_graph_change(user_message)
    graph_changed = _graph_signature(before_graph) != _graph_signature(after_graph)
    updated_at = after_graph.get("updated_at") if isinstance(after_graph, dict) else None
    validation = (
        validate_pipeline_graph(after_graph)
        if expected_graph_change and after_nodes > 0 and not fetch_error
        else {"valid": True, "issues": []}
    )
    validation_errors = validation_issue_messages(validation)

    if fetch_error:
        return {
            "status": "degraded",
            "guardrail_passed": False,
            "graph_safe_to_apply": False,
            "expected_graph_change": expected_graph_change,
            "graph_changed": graph_changed,
            "node_count": after_nodes,
            "edge_count": after_edges,
            "updated_at": updated_at,
            "message": f"Agent replied, but graph sync verification failed: {fetch_error}",
            "repaired": repaired,
            "validation_errors": validation_errors,
        }

    if expected_graph_change and graph_changed and not validation["valid"]:
        return {
            "status": "invalid",
            "guardrail_passed": False,
            "graph_safe_to_apply": False,
            "expected_graph_change": True,
            "graph_changed": True,
            "node_count": after_nodes,
            "edge_count": after_edges,
            "updated_at": updated_at,
            "message": (
                "The agent changed the graph, but the persisted result failed pipeline "
                "validation: " + "; ".join(validation_errors)
            ),
            "repaired": repaired,
            "validation_errors": validation_errors,
        }

    if expected_graph_change and not graph_changed:
        return {
            "status": "warning",
            "guardrail_passed": False,
            "graph_safe_to_apply": False,
            "expected_graph_change": True,
            "graph_changed": False,
            "node_count": after_nodes,
            "edge_count": after_edges,
            "updated_at": updated_at,
            "message": (
                "The request looked like it should change the canvas, but no visible graph "
                "change was persisted."
            ),
            "repaired": repaired,
            "validation_errors": validation_errors,
        }

    if graph_changed:
        return {
            "status": "synced",
            "guardrail_passed": True,
            "graph_safe_to_apply": True,
            "expected_graph_change": expected_graph_change,
            "graph_changed": True,
            "node_count": after_nodes,
            "edge_count": after_edges,
            "updated_at": updated_at,
            "message": (
                f"Canvas graph synced: {before_nodes}->{after_nodes} nodes, "
                f"{before_edges}->{after_edges} edges."
            ),
            "repaired": repaired,
            "validation_errors": [],
        }

    return {
        "status": "unchanged",
        "guardrail_passed": True,
        "graph_safe_to_apply": True,
        "expected_graph_change": expected_graph_change,
        "graph_changed": False,
        "node_count": after_nodes,
        "edge_count": after_edges,
        "updated_at": updated_at,
        "message": f"Canvas graph checked: {after_nodes} nodes and {after_edges} edges.",
        "repaired": repaired,
        "validation_errors": [],
    }


def _guardrail_repair_task(
    user_message: str,
    canvas_graph: dict | None,
    backend_graph: dict | None,
    validation_errors: list[str] | None = None,
) -> str:
    error_context = "\n".join(f"- {error}" for error in validation_errors or [])
    return (
        "Guardrail repair: the previous turn did not persist a complete valid pipeline "
        "for the user's request. Correct the persisted graph now. Use one mutating tool "
        "at a time. If the attempted new design is structurally wrong, delete its steps "
        "and rebuild them in actual dependency order. A destination must remain terminal, "
        "and rest-api tasks require a real endpoint. Verify the final graph with overview."
        + (f"\n\nVALIDATION ERRORS:\n{error_context}" if error_context else "")
        + "\n\n"
        + _build_agent_task(user_message, canvas_graph, backend_graph)
    )


@app.route("/agentic_generate_dockerfiles", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_dockerfiles():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}

    try:
        pipeline_graph = _pipeline_graph_from_payload_or_backend(data)
        files = _file_refs_from_version_graph(pipeline_graph)
        filenames, buckets, ids = _dockerfile_inputs(files)
        print("[analytics_api.py] Filenames received:", filenames)
        print("[analytics_api.py] Buckets received:", buckets)
        print("[analytics_api.py] Corresponding IDs to filenames that were received:", ids)

        print("[analytics_api.py] Deriving deployment files from deterministic runtime packages.")
        parsed = run_async(
            generate_dockerfiles_with_agent(
                filenames,
                ids,
                pipeline_graph=pipeline_graph,
                file_refs=files,
                require_attached_runtime=bool(data.get("require_attached_runtime")),
            )
        )
        response_payload = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed.dict()
        return jsonify(response_payload), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating dockerfiles:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/agentic_generate_yaml", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_yaml():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}
    dockerfile_json = data.get("dockerfile_json") or data.get("dockerfiles_json")
    print("[analytics_api.py] Dockerfile received:", dockerfile_json)

    try:
        print("[analytics_api.py] Generating Argo YAML with deterministic artifact builder.")
        pipeline_graph = _pipeline_graph_from_payload_or_backend(data)
        yaml_text = build_argo_workflow_yaml(pipeline_graph, dockerfile_json)
        resp = make_response(yaml_text, 200)
        resp.headers["Content-Type"] = "application/x-yaml; charset=utf-8"
        return resp
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating YAML:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/agentic_generate_dagster", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_dagster():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}
    dockerfile_json = data.get("dockerfile_json") or data.get("dockerfiles_json")

    try:
        print("[analytics_api.py] Generating Dagster project with deterministic artifact builder.")
        pipeline_graph = _pipeline_graph_from_payload_or_backend(data)
        files = build_dagster_project_files(pipeline_graph, dockerfile_json)
        return jsonify(
            {
                "files": files,
                "guardrails": {
                    "valid": True,
                    "checks": [
                        "Dagster project generated from persisted runtime artifacts",
                        "one defs.yaml component instance per executable pipeline step",
                        "graph edges mapped to Dagster asset dependencies",
                    ],
                },
            }
        ), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating Dagster project:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/agentic_generate_deployment_bundle", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_deployment_bundle():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}
    dockerfile_json = data.get("dockerfile_json") or data.get("dockerfiles_json")
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    validation_options = data.get("validation") if isinstance(data.get("validation"), dict) else {}
    validation_mode = str(
        validation_options.get("mode")
        or data.get("validation_mode")
        or data.get("deploymentValidationMode")
        or "validate"
    ).strip().lower()
    # Structural and input-contract validation is mandatory for every bundle.
    # Fast mode skips heavyweight target loading/materialization only.
    validate_bundle = True
    fast_validation = validation_mode == "fast"

    try:
        print("[analytics_api.py] Generating canonical deployment bundle.")
        pipeline_graph = _pipeline_graph_from_payload_or_backend(data)
        bundle = build_deployment_bundle_files(
            pipeline_graph,
            dockerfile_json,
            targets=targets,
        )

        validation_report = None
        repair_report = None
        run_record = None
        if validate_bundle:
            bundle_dir = _write_validation_bundle(bundle["files"])
            service_report = _post_deployment_validation_request(
                bundle_path=bundle_dir,
                targets=bundle["manifest"]["targets"],
                options={
                    **validation_options,
                    "mode": validation_mode,
                    **(
                        {
                            "materialize": False,
                            "validate_argo": False,
                            "validate_dagster": False,
                        }
                        if fast_validation
                        else {}
                    ),
                },
            )
            if validation_mode in {"repair", "validate-and-repair"}:
                repair_report = service_report.get("repair_report")
                validation_report = (
                    service_report.get("validation_report")
                    if isinstance(service_report.get("validation_report"), dict)
                    else service_report
                )
                if service_report.get("ok"):
                    repaired_files = _read_validation_bundle_files(bundle_dir)
                    if repaired_files:
                        bundle["files"] = repaired_files
            else:
                validation_report = service_report

            if not validation_report.get("ok"):
                return jsonify(
                    {
                        "error": "Deployment bundle validation failed",
                        "validation_report": validation_report,
                        "repair_report": repair_report,
                    }
                ), 422

            bundle["files"].append(
                {
                    "path": "validation/deployment-validation-report.json",
                    "filename": "deployment-validation-report.json",
                    "flow_id": "",
                    "content": json.dumps(validation_report, indent=2) + "\n",
                    "content_type": "application/json",
                    "role": "deployment-validation-report",
                }
            )
            if repair_report is not None:
                bundle["files"].append(
                    {
                        "path": "validation/deployment-repair-report.json",
                        "filename": "deployment-repair-report.json",
                        "flow_id": "",
                        "content": json.dumps(repair_report, indent=2) + "\n",
                        "content_type": "application/json",
                        "role": "deployment-repair-report",
                    }
                )

            execution_requested = (
                not fast_validation
                and bool(bundle["manifest"]["targets"].get("dagster"))
                and bool(validation_options.get("materialize", True))
            )
            if execution_requested:
                run_outputs = _read_run_output_files(bundle_dir)
                bundle["files"].extend(run_outputs)
                run_record = {
                    "schema_version": "inlumen.run-result@1",
                    "run_id": uuid.uuid4().hex,
                    "status": "succeeded",
                    "engine": "dagster",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "run_spec": "run-spec.json",
                    "package_manager": (
                        ((validation_report.get("dagster") or {}).get("package_manager"))
                        if isinstance(validation_report, dict)
                        else None
                    ) or "uv",
                    "outputs": [
                        {
                            "path": output["path"],
                            "filename": output["filename"],
                            "size_bytes": output.get("size_bytes"),
                            "sha256": output.get("sha256"),
                        }
                        for output in run_outputs
                    ],
                }
                bundle["files"].append(
                    {
                        "path": f"runs/{run_record['run_id']}/run-result.json",
                        "filename": "run-result.json",
                        "flow_id": "",
                        "content": json.dumps(run_record, indent=2) + "\n",
                        "content_type": "application/json",
                        "role": "run-result",
                    }
                )

        return jsonify(
            {
                **bundle,
                "validation_report": validation_report,
                "repair_report": repair_report,
                "run": run_record,
            }
        ), 200
    except DeploymentArtifactValidationError as exc:
        return jsonify(
            {
                "error": "Deployment bundle validation failed",
                "validation_report": {
                    "ok": False,
                    "errors": exc.errors,
                },
            }
        ), 422
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating deployment bundle:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/agentic_generate_version_yamls", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_version_yamls():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json(silent=True) or {}

    try:
        versions = run_async(fetch_pipeline_versions(
            include_graph=True,
            authorization=_request_authorization_header(),
        ))
        generated_versions = []

        for version in versions:
            graph = version.get("graph") if isinstance(version.get("graph"), dict) else {}
            file_refs = _file_refs_from_version_graph(graph)
            filenames, _buckets, ids = _dockerfile_inputs(file_refs) if file_refs else ([], [], [])
            dockerfiles = run_async(
                generate_dockerfiles_with_agent(
                    filenames,
                    ids,
                    pipeline_graph=graph,
                    file_refs=file_refs,
                )
            )
            dockerfiles_json = dockerfiles.model_dump() if hasattr(dockerfiles, "model_dump") else dockerfiles.dict()

            yaml_text = generate_argo_yaml_from_graph(
                pipeline_graph=graph,
                file_refs=file_refs,
                dockerfiles=dockerfiles_json,
            )
            generated_versions.append({
                "uid": str(version.get("uid") or ""),
                "name": str(version.get("name") or ""),
                "version": version.get("version"),
                "description": version.get("description"),
                "created_at": version.get("created_at"),
                "updated_at": version.get("updated_at"),
                "file_count": version.get("file_count"),
                "node_count": version.get("node_count"),
                "edge_count": version.get("edge_count"),
                "yaml": yaml_text,
            })

        return jsonify({"versions": generated_versions}), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating version YAMLs:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/simple_chat", methods=["POST", "OPTIONS"])
@app.route("/agentic_pipeline_editor", methods=["POST", "OPTIONS"])
@require_auth
def agentic_pipeline_editor():
    if request.method == "OPTIONS":
        return _preflight_response()

    payload = request.get_json(force=True) or {}
    user_message = (payload.get("user_message") or "").strip()
    if not user_message:
        return jsonify({"error": "Missing user_message"}), 400
    canvas_graph = _clean_client_graph(payload.get("canvas_graph"))
    active_version_uid = str(payload.get("active_version_uid") or payload.get("version_uid") or "main").strip() or "main"
    active_version_name = str(payload.get("active_version_name") or payload.get("version_name") or "").strip()
    if active_version_uid == "main":
        active_version_name = "Main"

    session_id = payload.get("session_id") or str(uuid.uuid4())
    try:
        llm_config = llm_config_from_payload(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    log_llm_selection("User message sent to pipeline editor", llm_config)
    authorization = _request_authorization_header()

    async def run_turn():
        before_graph, before_graph_error = await _safe_fetch_pipeline_graph()
        if before_graph_error:
            raise RuntimeError(f"Could not read the persisted pipeline before the agent turn: {before_graph_error}")
        if canvas_graph is not None:
            try:
                await sync_backend_to_canvas_graph(
                    canvas_graph,
                    active_version_uid,
                    active_version_name,
                    authorization=authorization,
                )
                before_graph, before_graph_error = await _safe_fetch_pipeline_graph()
            except Exception as exc:
                print("[analytics_api.py] Failed to reconcile backend to visible canvas:", exc)
                raise RuntimeError(
                    "The visible canvas could not be reconciled with the persisted graph; "
                    "the agent turn was not started."
                ) from exc
            if before_graph_error:
                raise RuntimeError(
                    f"Could not verify the reconciled canvas before the agent turn: {before_graph_error}"
                )

        visible_before_graph = canvas_graph or before_graph
        team = build_pipeline_editing_team(
            llm_config=llm_config,
            authorization=authorization,
            provenance_context={
                "user_query": user_message,
                "session_id": session_id,
            },
        )
        team_state = load_state_from_disk(session_id)
        if team_state:
            await team.load_state(team_state)
        result = await team.run(task=_build_agent_task(user_message, canvas_graph, before_graph))
        assistant_message = _assistant_message_from_result(result)
        after_graph, after_graph_error = await _safe_fetch_pipeline_graph()
        sync = _build_graph_sync_guardrail(
            visible_before_graph,
            after_graph,
            user_message,
            after_graph_error,
        )

        if (
            sync["expected_graph_change"]
            and not sync["guardrail_passed"]
            and not after_graph_error
        ):
            repair_result = await team.run(task=_guardrail_repair_task(
                user_message,
                canvas_graph,
                after_graph,
                sync.get("validation_errors"),
            ))
            repair_message = _assistant_message_from_result(repair_result)
            if repair_message:
                assistant_message = repair_message
            repaired_graph, repaired_graph_error = await _safe_fetch_pipeline_graph()
            after_graph = repaired_graph
            sync = _build_graph_sync_guardrail(
                visible_before_graph,
                after_graph,
                user_message,
                repaired_graph_error,
                repaired=True,
            )

        if sync["expected_graph_change"] and not sync["guardrail_passed"]:
            failed_messages = list(sync.get("validation_errors") or [])
            rollback_error = None
            try:
                if not isinstance(visible_before_graph, dict):
                    raise RuntimeError("No pre-turn graph snapshot is available.")
                await sync_backend_to_canvas_graph(
                    visible_before_graph,
                    active_version_uid,
                    active_version_name,
                    authorization=authorization,
                )
                rollback_graph, rollback_fetch_error = await _safe_fetch_pipeline_graph()
                if rollback_fetch_error or not isinstance(rollback_graph, dict):
                    raise RuntimeError(rollback_fetch_error or "Rollback graph could not be read.")
                after_graph = rollback_graph
            except Exception as exc:
                rollback_error = str(exc)
                print("[analytics_api.py] Failed to roll back rejected agent graph:", exc)

            rollback_nodes, rollback_edges = _graph_counts(after_graph)
            reason = "; ".join(failed_messages) or sync.get("message") or "No valid graph change was persisted."
            sync.update({
                "status": "rejected",
                "guardrail_passed": False,
                "graph_safe_to_apply": False,
                "rollback_applied": rollback_error is None,
                "node_count": rollback_nodes,
                "edge_count": rollback_edges,
                "updated_at": after_graph.get("updated_at") if isinstance(after_graph, dict) else None,
                "message": (
                    f"The agent result was rejected and the pre-turn pipeline was preserved. {reason}"
                    if rollback_error is None
                    else f"The agent result was invalid and automatic rollback also failed: {rollback_error}"
                ),
            })
            assistant_message = (
                "I couldn't safely apply that pipeline design, so I preserved the pipeline "
                f"from before this request. Validation details: {reason}"
            )

        if sync["guardrail_passed"] and isinstance(after_graph, dict):
            pipeline = after_graph.get("pipeline") if isinstance(after_graph.get("pipeline"), dict) else {}
            version_uid_to_save = active_version_uid or str(pipeline.get("active_version_uid") or "main")
            version_name_to_save = active_version_name or str(pipeline.get("active_version_name") or pipeline.get("version") or "")
            if version_uid_to_save == "main":
                version_name_to_save = "Main"
            try:
                await save_active_pipeline_version(
                    after_graph,
                    version_uid_to_save,
                    version_name_to_save,
                    authorization=authorization,
                )
            except Exception as exc:
                print("[analytics_api.py] Failed to persist agent graph to active version:", exc)
                sync["message"] = (
                    (sync.get("message") or "Agent graph sync completed.")
                    + f" Active version save failed: {exc}"
                )
                sync["guardrail_passed"] = False
                # The live graph itself is still validated and safe to show even
                # though its version snapshot could not be updated.
                sync["graph_safe_to_apply"] = True

        if sync["guardrail_passed"]:
            new_state = await team.save_state()
            save_state_to_disk(session_id, new_state)
        else:
            clear_state_from_disk(session_id)
        return assistant_message, after_graph, sync

    try:
        assistant_message, graph, sync = asyncio.run(run_turn())
        return jsonify({
            "session_id": session_id,
            "assistant_message": assistant_message,
            "graph": graph,
            "sync": sync,
        }), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/simple_chat/reset", methods=["POST", "OPTIONS"])
@app.route("/agentic_pipeline_editor/reset", methods=["POST", "OPTIONS"])
@require_auth
def agentic_pipeline_editor_reset():
    if request.method == "OPTIONS":
        return _preflight_response()

    payload = request.get_json(force=True) or {}
    session_id = payload.get("session_id")
    if session_id:
        clear_state_from_disk(session_id)
    return jsonify({"ok": True}), 200
