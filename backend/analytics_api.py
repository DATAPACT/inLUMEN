import asyncio
import base64
import json
import mimetypes
import os
import re
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, has_request_context, make_response, request

from async_runtime import run_async
from auth_middleware import require_auth
from chat_state import clear_state_from_disk, load_state_from_disk, save_state_to_disk
from deployment_artifacts import (
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
        content = str(file_entry.get("content") or "")
        if str(file_entry.get("encoding") or "") == "base64":
            destination.write_bytes(base64.b64decode(content))
        else:
            destination.write_text(content, encoding="utf-8")
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
        raw_content = path.read_bytes()
        try:
            content = raw_content.decode("utf-8")
            encoding = ""
        except UnicodeDecodeError:
            content = base64.b64encode(raw_content).decode("ascii")
            encoding = "base64"
        files.append(
            {
                "path": relative,
                "filename": path.name,
                "flow_id": "",
                "content": content,
                "content_type": (
                    "application/json"
                    if path.suffix == ".json"
                    else "application/x-yaml;charset=utf-8"
                    if path.suffix in {".yaml", ".yml"}
                    else mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream"
                ),
                "role": "runtime",
                **({"encoding": encoding} if encoding else {}),
            }
        )
    return files


def _optional_llm_config_from_payload(data: dict):
    raw_config = data.get("llm_config")
    has_top_level_config = any(
        key in data
        for key in (
            "provider",
            "llm_provider",
            "model",
            "base_url",
            "baseUrl",
            "api_key",
            "apiKey",
        )
    )
    if isinstance(raw_config, dict) or has_top_level_config:
        return llm_config_from_payload(data)
    return None


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

    nodes = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nodes.append(_node_payload(node))

    edges = []
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        edges.append({
            "source": str(edge.get("source", "")),
            "target": str(edge.get("target", "")),
        })

    nodes.sort(key=lambda node: node["id"])
    edges.sort(key=lambda edge: (edge["source"], edge["target"]))
    return json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True)


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
        files = node_data.get("files")
        if isinstance(files, list):
            payload["files"] = [_clip_text(item, 200) for item in files if str(item or "").strip()]
        param = node_data.get("param")
        if isinstance(param, dict):
            payload["param"] = {
                _clip_text(key, 100): _clip_text(val, 300)
                for key, val in param.items()
                if str(key or "").strip()
            }
        cleaned_nodes.append(payload)

    cleaned_edges = []
    seen_edge_keys: set[tuple[str, str]] = set()
    for raw_edge in value.get("edges") or []:
        if not isinstance(raw_edge, dict):
            continue
        source = str(raw_edge.get("source", "")).strip()
        target = str(raw_edge.get("target", "")).strip()
        edge_key = (source, target)
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
        cleaned_edges.append({"source": source, "target": target})

    return {
        "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None,
        "nodes": cleaned_nodes,
        "edges": cleaned_edges,
    }


def _graph_for_agent_context(graph: dict | None) -> dict:
    if not isinstance(graph, dict):
        return {"node_count": 0, "edge_count": 0, "nodes": [], "edges": []}
    cleaned = _clean_client_graph(graph) or graph
    nodes = cleaned.get("nodes") if isinstance(cleaned.get("nodes"), list) else []
    edges = cleaned.get("edges") if isinstance(cleaned.get("edges"), list) else []
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

    if fetch_error:
        return {
            "status": "degraded",
            "guardrail_passed": False,
            "expected_graph_change": expected_graph_change,
            "graph_changed": graph_changed,
            "node_count": after_nodes,
            "edge_count": after_edges,
            "updated_at": updated_at,
            "message": f"Agent replied, but graph sync verification failed: {fetch_error}",
            "repaired": repaired,
        }

    if expected_graph_change and not graph_changed:
        return {
            "status": "warning",
            "guardrail_passed": False,
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
        }

    if graph_changed:
        return {
            "status": "synced",
            "guardrail_passed": True,
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
        }

    return {
        "status": "unchanged",
        "guardrail_passed": True,
        "expected_graph_change": expected_graph_change,
        "graph_changed": False,
        "node_count": after_nodes,
        "edge_count": after_edges,
        "updated_at": updated_at,
        "message": f"Canvas graph checked: {after_nodes} nodes and {after_edges} edges.",
        "repaired": repaired,
    }


def _guardrail_repair_task(
    user_message: str,
    canvas_graph: dict | None,
    backend_graph: dict | None,
) -> str:
    return (
        "Guardrail repair: the previous turn did not persist a visible pipeline graph "
        "change, but the user request appears to require one. Use the pipeline tools now "
        "to create, update, delete, or connect STEP nodes in Neo4j as needed. "
        "If no design pipeline exists, create one first.\n\n"
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

        llm_config = _optional_llm_config_from_payload(data)
        if llm_config is not None:
            log_llm_selection("Generating Dockerfiles from pipeline context", llm_config)
        print("[analytics_api.py] Generating Dockerfiles from registered generators and generic fallback.")
        parsed = run_async(
            generate_dockerfiles_with_agent(
                filenames,
                ids,
                llm_config,
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
        or "fast"
    ).strip().lower()
    validate_bundle = bool(
        validation_mode in {"validate", "repair", "validate-and-repair"}
        or
        data.get("validate_bundle")
        or data.get("validateDeploymentBundle")
        or validation_options.get("enabled")
    )

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
        if validate_bundle:
            bundle_dir = _write_validation_bundle(bundle["files"])
            service_report = _post_deployment_validation_request(
                bundle_path=bundle_dir,
                targets=bundle["manifest"]["targets"],
                options={
                    **validation_options,
                    "mode": validation_mode,
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

        return jsonify(
            {
                **bundle,
                "validation_report": validation_report,
                "repair_report": repair_report,
            }
        ), 200
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
        llm_config = _optional_llm_config_from_payload(data)
        if llm_config is not None:
            log_llm_selection("Generating Dockerfiles for all pipeline versions", llm_config)
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
                    llm_config,
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
        canvas_sync_error = None
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
                canvas_sync_error = str(exc)
                print("[analytics_api.py] Failed to reconcile backend to visible canvas:", exc)

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
            canvas_sync_error or before_graph_error or after_graph_error,
        )

        if (
            sync["expected_graph_change"]
            and not sync["guardrail_passed"]
            and not canvas_sync_error
            and not before_graph_error
            and not after_graph_error
        ):
            repair_result = await team.run(task=_guardrail_repair_task(user_message, canvas_graph, after_graph))
            repair_message = _assistant_message_from_result(repair_result)
            if repair_message:
                assistant_message = repair_message
            repaired_graph, repaired_graph_error = await _safe_fetch_pipeline_graph()
            after_graph = repaired_graph
            sync = _build_graph_sync_guardrail(
                visible_before_graph,
                after_graph,
                user_message,
                before_graph_error or repaired_graph_error,
                repaired=True,
            )

        if isinstance(after_graph, dict):
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

        new_state = await team.save_state()
        save_state_to_disk(session_id, new_state)
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
