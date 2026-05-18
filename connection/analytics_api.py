import asyncio
import json
import os
import re
import uuid

from flask import Flask, jsonify, make_response, request

from async_runtime import run_async
from auth_middleware import require_auth
from chat_state import clear_state_from_disk, load_state_from_disk, save_state_to_disk
from deployment_agents import generate_dockerfiles_with_agent, build_argo_yaml_team
from graph_client import (
    fetch_pipeline_graph,
    save_active_pipeline_version,
    sync_backend_to_canvas_graph,
)
from llm_config import llm_config_from_payload, log_llm_selection
from pipeline_editor_team import build_pipeline_editing_team
from runtime_config import default_frontend_origin, get_service_port


NEO4J_API_PORT = get_service_port("NEO4J_API_PORT", 5001)
LLM_API_PORT = get_service_port("LLM_API_PORT", 5002)
NEO4J_API_BASE_URL = (
    os.getenv("NEO4J_API_BASE_URL", "").strip()
    or f"http://127.0.0.1:{NEO4J_API_PORT}"
)
CORS_ALLOWED_ORIGIN = (
    os.getenv("CORS_ALLOWED_ORIGIN", "").strip()
    or default_frontend_origin()
)

app = Flask(__name__)


def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = CORS_ALLOWED_ORIGIN
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@app.after_request
def apply_cors(response):
    return add_cors_headers(response)


def _preflight_response():
    return make_response("", 200)


def _dockerfile_inputs(files: list[dict]) -> tuple[list[str], list[str], list[str]]:
    filenames = [file["filename"] for file in files]
    buckets = [file["bucket"] for file in files]
    ids = []
    for bucket in buckets:
        match = re.search(r"files-step-id-(\d+)", bucket)
        if not match:
            raise ValueError(f"Could not extract step id from bucket '{bucket}'.")
        ids.append(match.group(1))
    return filenames, buckets, ids


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


def _build_agent_task(
    user_message: str,
    canvas_graph: dict | None,
    backend_graph: dict | None,
) -> str:
    return (
        f"{user_message}\n\n"
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
        return await fetch_pipeline_graph(NEO4J_API_BASE_URL), None
    except Exception as exc:
        print("[analytics_api.py] Failed to fetch pipeline graph for sync guardrail:", exc)
        return None, str(exc)


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
    files = data.get("files", [])

    try:
        filenames, buckets, ids = _dockerfile_inputs(files)
        print("[analytics_api.py] Filenames received:", filenames)
        print("[analytics_api.py] Buckets received:", buckets)
        print("[analytics_api.py] Corresponding IDs to filenames that were received:", ids)

        llm_config = llm_config_from_payload(data)
        log_llm_selection("Generating Dockerfiles", llm_config)
        parsed = run_async(
            generate_dockerfiles_with_agent(filenames, ids, llm_config)
        )
        return jsonify(parsed.model_dump()), 200
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
    task = data.get(
        "task",
        "Generate an Argo Workflow YAML file based on the given pipeline design.",
    )
    task_message = task
    if dockerfile_json:
        try:
            dockerfile_dump = json.dumps(dockerfile_json)
        except Exception:
            dockerfile_dump = str(dockerfile_json)
        task_message += "\n\nDockerfile metadata: " + dockerfile_dump

    async def run_team():
        llm_config = llm_config_from_payload(data)
        log_llm_selection("Generating Argo YAML", llm_config)
        team = build_argo_yaml_team(llm_config, NEO4J_API_BASE_URL)
        result = await team.run(task=task_message)
        print("[analytics_api.py] build_argo_yaml_team run result messages:")
        for idx, msg in enumerate(result.messages or []):
            source = getattr(msg, "source", None)
            content = getattr(msg, "content", None)
            print(f"  message[{idx}] source={source} content_preview={str(content)[:200]}")
        return _assistant_message_from_result(result)

    try:
        yaml_text = asyncio.run(run_team())
        resp = make_response(yaml_text, 200)
        resp.headers["Content-Type"] = "application/x-yaml; charset=utf-8"
        return resp
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating YAML:", exc)
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

    async def run_turn():
        before_graph, before_graph_error = await _safe_fetch_pipeline_graph()
        canvas_sync_error = None
        if canvas_graph is not None:
            try:
                await sync_backend_to_canvas_graph(
                    NEO4J_API_BASE_URL,
                    canvas_graph,
                    active_version_uid,
                    active_version_name,
                )
                before_graph, before_graph_error = await _safe_fetch_pipeline_graph()
            except Exception as exc:
                canvas_sync_error = str(exc)
                print("[analytics_api.py] Failed to reconcile backend to visible canvas:", exc)

        visible_before_graph = canvas_graph or before_graph
        team = build_pipeline_editing_team(
            llm_config=llm_config,
            neo4j_api_base_url=NEO4J_API_BASE_URL,
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
                    NEO4J_API_BASE_URL,
                    after_graph,
                    version_uid_to_save,
                    version_name_to_save,
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=LLM_API_PORT)
