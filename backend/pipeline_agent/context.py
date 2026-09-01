"""Context shaping and presentation helpers for pipeline-agent turns."""

import json
import re


def _assistant_message_from_result(result) -> str:
    """Select only a real model text message, never an agent tool event.

    AutoGen task results also contain tool-call request, execution, and summary
    messages. Those objects may expose a string ``content`` field, but that
    content is protocol data and must never become chat text.
    """
    for msg in reversed(result.messages or []):
        message_type = getattr(msg, "type", type(msg).__name__)
        content = getattr(msg, "content", None)
        if message_type == "TextMessage" and isinstance(content, str):
            return content
    return ""


INTERNAL_AGENT_MESSAGE_PATTERNS = (
    re.compile(
        r"(?:^|[\s`])(?:call\s*:\s*)?"
        r"(?:create|configure|connect|disconnect|insert|delete|list|get|inspect|overview)"
        r"_[a-z0-9_]+\s*\(",
        re.IGNORECASE,
    ),
    re.compile(
        r"\{\s*[\"']tool[\"']\s*:\s*[\"']"
        r"(?:create|configure|connect|disconnect|insert|delete|list|get|inspect|overview)"
        r"_[a-z0-9_]+[\"']\s*,\s*[\"']params[\"']\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"[\"'](?:name|tool_name)[\"']\s*:\s*[\"']"
        r"(?:create|configure|connect|disconnect|insert|delete|list|get|inspect|overview)"
        r"_[a-z0-9_]+[\"']\s*,\s*[\"'](?:arguments|params)[\"']\s*:",
        re.IGNORECASE,
    ),
    re.compile(r"<(?:tool_call|function_call)\b", re.IGNORECASE),
    re.compile(
        r"[\"'](?:implementation_json|param_json|ports_json|secret_params_json|"
        r"pipeline_updated_at|step_link|files_linked_to_step)[\"']\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"\[\s*\{\s*[\"'](?:connection|deleted_step|disconnected|flow_step|"
        r"reusable_pipeline|subpipeline_step)[\"']\s*:",
        re.IGNORECASE,
    ),
)

SAFE_INTERNAL_OUTPUT_MESSAGE = (
    "I couldn't display that response because it contained internal operation data."
)


def _looks_like_internal_agent_message(message: object) -> bool:
    """Detect tool calls, tool results, and persisted graph records anywhere."""
    text = str(message or "").strip()
    if not text:
        return True
    return any(pattern.search(text) for pattern in INTERNAL_AGENT_MESSAGE_PATTERNS)


def _graph_summary_message(graph: dict | None) -> str:
    """Build safe deterministic prose from user-visible persisted graph fields."""
    cleaned = _clean_client_graph(graph) if isinstance(graph, dict) else None
    if not cleaned:
        return SAFE_INTERNAL_OUTPUT_MESSAGE
    nodes = cleaned.get("nodes") if isinstance(cleaned.get("nodes"), list) else []
    edges = cleaned.get("edges") if isinstance(cleaned.get("edges"), list) else []
    if not nodes:
        return "The pipeline design is empty."

    pipeline = graph.get("pipeline") if isinstance(graph.get("pipeline"), dict) else {}
    pipeline_label = _clip_text(
        pipeline.get("label") or pipeline.get("name") or "Pipeline",
        160,
    )
    labels = {
        str(node.get("id") or ""): _clip_text(node.get("label") or node.get("id"), 120)
        for node in nodes
        if str(node.get("id") or "")
    }
    node_by_id = {
        str(node.get("id") or ""): node
        for node in nodes
        if str(node.get("id") or "")
    }
    branches = []
    for edge in edges:
        source_port = str(edge.get("source_port") or "")
        if source_port not in {"when_true", "when_false"}:
            continue
        source_id = str(edge.get("source") or "")
        target_id = str(edge.get("target") or "")
        source = node_by_id.get(source_id, {})
        if str(source.get("type") or "").lower() != "flow":
            continue
        branches.append(
            f"{labels.get(source_id, source_id)} {source_port} -> "
            f"{labels.get(target_id, target_id)}"
        )
    component_word = "component" if len(nodes) == 1 else "components"
    connection_word = "connection" if len(edges) == 1 else "connections"
    summary = (
        f"Current pipeline design: {pipeline_label} contains {len(nodes)} "
        f"{component_word} and {len(edges)} {connection_word}."
    )
    if branches:
        summary += " Condition branches: " + "; ".join(branches[:8]) + "."
    return summary


def _safe_assistant_message(message: object, graph: dict | None = None) -> str:
    """Return model prose or a graph-derived notice; never expose protocol data."""
    text = str(message or "").strip()
    if text and not _looks_like_internal_agent_message(text):
        return text
    return _graph_summary_message(graph)


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
        # Keep the high-level category visible, but hide every runtime
        # configuration and implementation detail from the design-only agent.
        summary.pop("content", None)
        summary.pop("endpoint", None)
        summary.pop("database", None)
        if template:
            summary["template"] = _clip_text(template, 160)
        if node.get("configuration_status"):
            summary["configuration_status"] = _clip_text(
                node.get("configuration_status"),
                80,
            )
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
