from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from typing import Any

from node_ports import ports_for_template
from step_types import normalize_step_type


def _node_data(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    data = node.get("data")
    return data if isinstance(data, dict) else node


def _node_id(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    data = _node_data(node)
    return str(node.get("id") or data.get("id") or "").strip()


def _implementation(data: dict[str, Any]) -> dict[str, Any]:
    implementation = data.get("implementation")
    if isinstance(implementation, dict) and implementation:
        return implementation

    parameters = data.get("param") or data.get("parameters")
    if not isinstance(parameters, dict):
        encoded = data.get("param_json")
        try:
            parameters = json.loads(encoded) if isinstance(encoded, str) else {}
        except (TypeError, ValueError):
            parameters = {}
    model_plan = parameters.get("model_plan") if isinstance(parameters, dict) else None
    return model_plan if isinstance(model_plan, dict) else {}


def _parameters(data: dict[str, Any]) -> dict[str, Any]:
    parameters = data.get("param") or data.get("parameters")
    if isinstance(parameters, dict):
        return parameters
    encoded = data.get("param_json")
    try:
        parsed = json.loads(encoded) if isinstance(encoded, str) else {}
    except (TypeError, ValueError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _issue(
    category: str,
    code: str,
    message: str,
    *,
    node_id: str = "",
    edge_id: str = "",
) -> dict[str, str]:
    issue = {
        "severity": "error",
        "category": category,
        "code": code,
        "message": message,
    }
    if node_id:
        issue["node_id"] = node_id
    if edge_id:
        issue["edge_id"] = edge_id
    return issue


def _port_types_compatible(source: object, target: object) -> bool:
    left = str(source or "").strip().lower()
    right = str(target or "").strip().lower()
    wildcard = {"", "any", "unknown", "*"}
    return left in wildcard or right in wildcard or left == right


FLOW_EXPRESSION_PATTERN = re.compile(
    r'^value(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[(?:\d+|"[^"]+"|\'[^\']+\')\]))*'
    r'(?:\s*(?:==|!=|>=|<=|>|<)\s*(?:"[^"]*"|\'[^\']*\'|-?\d+(?:\.\d+)?|true|false|null))?$'
)


def validate_pipeline_graph(graph: Any) -> dict[str, Any]:
    """Validate the persisted graph contract before it is returned to the canvas."""
    if not isinstance(graph, dict):
        return {
            "valid": False,
            "issues": [_issue("graph", "missing-graph", "No pipeline graph was persisted.")],
        }

    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    issues: list[dict[str, str]] = []
    node_by_id: dict[str, dict[str, Any]] = {}
    kinds: dict[str, str] = {}
    ports_by_node: dict[str, dict[str, list[dict[str, Any]]]] = {}

    for node in nodes:
        node_id = _node_id(node)
        if not node_id:
            issues.append(_issue("graph", "missing-node-id", "A component has no node id."))
            continue
        if node_id in node_by_id:
            issues.append(_issue(
                "graph",
                "duplicate-node-id",
                f"Component id {node_id!r} is duplicated.",
                node_id=node_id,
            ))
            continue

        data = _node_data(node)
        kind = normalize_step_type(data.get("type"), default="task")
        template_label = str(data.get("template_label") or "").strip()
        ports = ports_for_template(
            data.get("ports") or data.get("ports_json"),
            kind,
            template_label,
        )
        node_by_id[node_id] = data
        kinds[node_id] = kind
        ports_by_node[node_id] = ports

        if kind == "flow":
            parameters = _parameters(data)
            if template_label not in {"Condition", "Parallel Map"}:
                issues.append(_issue(
                    "configuration",
                    "missing-flow-behavior",
                    "Choose a Flow behavior: Condition or Parallel Map.",
                    node_id=node_id,
                ))
            elif template_label == "Condition":
                expression = str(parameters.get("expression") or "").strip()
                if not expression:
                    issues.append(_issue(
                        "configuration",
                        "missing-required-parameter",
                        "Condition requires parameter 'expression'.",
                        node_id=node_id,
                    ))
                elif not FLOW_EXPRESSION_PATTERN.fullmatch(expression):
                    issues.append(_issue(
                        "configuration",
                        "invalid-flow-expression",
                        "Condition expressions must compare value (or value.field) with a literal.",
                        node_id=node_id,
                    ))
            else:
                concurrency = parameters.get("max_concurrency")
                if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
                    issues.append(_issue(
                        "configuration",
                        "invalid-flow-concurrency",
                        "Parallel Map maximum concurrency must be a positive whole number.",
                        node_id=node_id,
                    ))
                if str(parameters.get("failure_policy") or "") not in {"stop", "continue"}:
                    issues.append(_issue(
                        "configuration",
                        "invalid-flow-failure-policy",
                        "Parallel Map requires a valid item failure policy.",
                        node_id=node_id,
                    ))

        if kind == "task":
            implementation = _implementation(data)
            if not implementation:
                issues.append(_issue(
                    "implementation",
                    "missing-implementation",
                    "Task implementation is not configured.",
                    node_id=node_id,
                ))
                continue
            implementation_kind = str(implementation.get("kind") or "").strip().lower()
            implementation_kind = (
                "repository" if implementation_kind == "git-repository" else implementation_kind
            )
            if not implementation_kind:
                issues.append(_issue(
                    "implementation",
                    "missing-implementation-kind",
                    "Task implementation does not identify an implementation kind.",
                    node_id=node_id,
                ))
            if implementation_kind == "rest-api" and not str(
                implementation.get("endpoint") or data.get("endpoint") or ""
            ).strip():
                issues.append(_issue(
                    "implementation",
                    "missing-endpoint",
                    "REST API implementation requires an endpoint.",
                    node_id=node_id,
                ))
            if implementation_kind == "container" and not str(
                implementation.get("image") or ""
            ).strip():
                issues.append(_issue(
                    "implementation",
                    "missing-container-image",
                    "Container implementation requires an image.",
                    node_id=node_id,
                ))
            if implementation_kind == "repository" and not str(
                implementation.get("repository") or implementation.get("url") or ""
            ).strip():
                issues.append(_issue(
                    "implementation",
                    "missing-repository",
                    "Repository implementation requires a repository URL.",
                    node_id=node_id,
                ))

    if nodes and not any(kind == "source" for kind in kinds.values()):
        issues.append(_issue("graph", "missing-source", "A runnable pipeline requires a source."))
    if nodes and not any(kind == "destination" for kind in kinds.values()):
        issues.append(_issue(
            "graph",
            "missing-destination",
            "A runnable pipeline requires a terminal destination.",
        ))

    incoming: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    seen_connections: set[tuple[str, str, str, str]] = set()

    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            issues.append(_issue("graph", "invalid-edge", "A connection is not an object."))
            continue
        edge_id = str(edge.get("id") or f"edge-{index + 1}")
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        source_port_id = str(edge.get("sourceHandle") or edge.get("source_port") or "").strip()
        target_port_id = str(edge.get("targetHandle") or edge.get("target_port") or "").strip()

        if source not in node_by_id or target not in node_by_id:
            issues.append(_issue(
                "graph",
                "orphan-edge",
                "Connection references a missing component.",
                edge_id=edge_id,
            ))
            continue
        if source == target:
            issues.append(_issue(
                "graph",
                "self-edge",
                "A component cannot connect to itself.",
                node_id=source,
                edge_id=edge_id,
            ))
            continue

        key = (source, target, source_port_id, target_port_id)
        if key in seen_connections:
            issues.append(_issue(
                "graph",
                "duplicate-edge",
                "Duplicate port connection.",
                edge_id=edge_id,
            ))
            continue
        seen_connections.add(key)
        outgoing[source].add(target)
        incoming[target].add(source)

        if not source_port_id or not target_port_id:
            issues.append(_issue(
                "graph",
                "missing-edge-port",
                "Connection must identify both source and target ports.",
                node_id=target,
                edge_id=edge_id,
            ))
            continue

        source_port = next(
            (
                port
                for port in ports_by_node[source]["outputs"]
                if str(port.get("id") or "") == source_port_id
            ),
            None,
        )
        target_port = next(
            (
                port
                for port in ports_by_node[target]["inputs"]
                if str(port.get("id") or "") == target_port_id
            ),
            None,
        )
        if source_port is None or target_port is None:
            issues.append(_issue(
                "graph",
                "unknown-edge-port",
                "Connection references a port that does not exist.",
                node_id=target,
                edge_id=edge_id,
            ))
            continue
        if not _port_types_compatible(source_port.get("type"), target_port.get("type")):
            issues.append(_issue(
                "ports",
                "incompatible-port-types",
                f"Port types are incompatible: {source_port.get('type')} -> {target_port.get('type')}.",
                node_id=target,
                edge_id=edge_id,
            ))

    for node_id, ports in ports_by_node.items():
        for port in ports["inputs"]:
            if not port.get("required"):
                continue
            port_id = str(port.get("id") or "")
            connected = any(
                str(edge.get("target") or "") == node_id
                and str(edge.get("targetHandle") or edge.get("target_port") or "") == port_id
                for edge in edges
                if isinstance(edge, dict)
            )
            if not connected:
                issues.append(_issue(
                    "ports",
                    "missing-required-input",
                    f"Required input {str(port.get('name') or port_id)!r} is not connected.",
                    node_id=node_id,
                ))
        if kinds.get(node_id) == "flow":
            for port in ports["outputs"]:
                if not port.get("required"):
                    continue
                port_id = str(port.get("id") or "")
                connected = any(
                    str(edge.get("source") or "") == node_id
                    and str(edge.get("sourceHandle") or edge.get("source_port") or "") == port_id
                    for edge in edges
                    if isinstance(edge, dict)
                )
                if not connected:
                    issues.append(_issue(
                        "ports",
                        "missing-required-flow-output",
                        f"Required Flow output {str(port.get('name') or port_id)!r} is not connected.",
                        node_id=node_id,
                    ))

    indegree = {node_id: len(incoming[node_id]) for node_id in node_by_id}
    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        for target in outgoing[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if node_by_id and visited != len(node_by_id):
        issues.append(_issue("graph", "cycle", "Pipeline connections must form an acyclic graph."))

    return {"valid": not issues, "issues": issues}


def validation_issue_messages(report: dict[str, Any], limit: int = 8) -> list[str]:
    issues = report.get("issues") if isinstance(report, dict) else []
    if not isinstance(issues, list):
        return []
    messages = []
    for issue in issues[:limit]:
        if not isinstance(issue, dict):
            continue
        node = str(issue.get("node_id") or "").strip()
        prefix = f"Node {node}: " if node else ""
        messages.append(prefix + str(issue.get("message") or issue.get("code") or "Invalid graph"))
    return messages
