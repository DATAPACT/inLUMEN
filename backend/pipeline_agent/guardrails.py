"""Validation and repair policy for pipeline-agent graph mutations."""

from collections import defaultdict, deque

from pipeline_agent.context import (
    _build_agent_task,
    _graph_counts,
    _graph_signature,
)
from pipeline_graph_validation import (
    validate_pipeline_graph,
    validation_issue_messages,
)


def _node_data(node: object) -> dict:
    if not isinstance(node, dict):
        return {}
    data = node.get("data")
    return data if isinstance(data, dict) else node


def _requested_branch_contract(user_message: str) -> dict[str, bool]:
    """Extract only explicit, high-confidence branch requirements from prose."""
    text = " ".join(str(user_message or "").lower().split())
    clean_and_outlier = (
        ("clean-data branch" in text or "clean data branch" in text)
        and "outlier branch" in text
    )
    explicit_condition_branches = (
        ("when_true" in text and "when_false" in text)
        or clean_and_outlier
    )
    two_branches = (
        "two branches" in text
        or "two branch" in text
        or explicit_condition_branches
    )
    separate_outputs = (
        "separate csv" in text
        or "separate output" in text
        or (clean_and_outlier and ("save" in text or "write" in text))
    )
    return {
        "two_branches": two_branches,
        "two_condition_branches": explicit_condition_branches,
        "distinct_destinations": two_branches and separate_outputs,
    }


def _requested_topology_errors(graph: dict | None, user_message: str) -> list[str]:
    """Verify explicit branch requirements against the persisted graph."""
    contract = _requested_branch_contract(user_message)
    if not any(contract.values()) or not isinstance(graph, dict):
        return []

    raw_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    raw_edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    nodes: dict[str, dict] = {}
    for node in raw_nodes:
        data = _node_data(node)
        node_id = str(
            (node.get("id") if isinstance(node, dict) else "")
            or data.get("id")
            or ""
        ).strip()
        if node_id:
            nodes[node_id] = data

    outgoing: dict[str, list[dict]] = defaultdict(list)
    incoming: dict[str, list[dict]] = defaultdict(list)
    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source not in nodes or target not in nodes:
            continue
        normalized = {
            "source": source,
            "target": target,
            "source_port": str(
                edge.get("sourceHandle") or edge.get("source_port") or ""
            ).strip(),
        }
        outgoing[source].append(normalized)
        incoming[target].append(normalized)

    conditions = [
        node_id
        for node_id, data in nodes.items()
        if str(data.get("type") or "").lower() == "flow"
        and str(data.get("template_label") or data.get("template") or "").lower()
        == "condition"
    ]
    candidates = []
    for condition_id in conditions:
        true_edges = [
            edge for edge in outgoing[condition_id]
            if edge["source_port"] == "when_true"
        ]
        false_edges = [
            edge for edge in outgoing[condition_id]
            if edge["source_port"] == "when_false"
        ]
        if true_edges and false_edges:
            candidates.append((condition_id, true_edges, false_edges))

    errors: list[str] = []
    if contract["two_condition_branches"] and not candidates:
        errors.append(
            "The request explicitly requires both Condition branches, but no "
            "Condition has connected when_true and when_false outputs."
        )
        return errors

    generic_branches = [
        (source_id, branch_edges)
        for source_id, branch_edges in outgoing.items()
        if len({edge["target"] for edge in branch_edges}) >= 2
    ]
    if (
        contract["two_branches"]
        and not contract["two_condition_branches"]
        and not generic_branches
    ):
        errors.append(
            "The request explicitly requires two downstream branches, but no "
            "component has two connected branch targets."
        )
        return errors

    def reachable_destinations(start: str) -> set[str]:
        found: set[str] = set()
        visited: set[str] = set()
        queue = deque([start])
        while queue:
            node_id = queue.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)
            if str(nodes[node_id].get("type") or "").lower() == "destination":
                found.add(node_id)
            queue.extend(edge["target"] for edge in outgoing[node_id])
        return found

    condition_topology_satisfied = False
    bypass_labels: set[str] = set()
    for condition_id, true_edges, false_edges in candidates:
        true_destinations = set().union(*(
            reachable_destinations(edge["target"]) for edge in true_edges
        ))
        false_destinations = set().union(*(
            reachable_destinations(edge["target"]) for edge in false_edges
        ))
        if (
            not contract["distinct_destinations"]
            or (
                true_destinations
                and false_destinations
                and true_destinations.isdisjoint(false_destinations)
            )
        ):
            condition_topology_satisfied = True

        ancestors: set[str] = set()
        queue = deque(edge["source"] for edge in incoming[condition_id])
        while queue:
            node_id = queue.popleft()
            if node_id in ancestors:
                continue
            ancestors.add(node_id)
            queue.extend(edge["source"] for edge in incoming[node_id])
        for branch_edge in [*true_edges, *false_edges]:
            target_id = branch_edge["target"]
            if any(
                edge["source"] != condition_id and edge["source"] in ancestors
                for edge in incoming[target_id]
            ):
                bypass_labels.add(
                    str(nodes[target_id].get("label") or target_id).strip()
                )

    generic_topology_satisfied = False
    if not contract["two_condition_branches"]:
        for _source_id, branch_edges in generic_branches:
            destinations_by_branch = [
                reachable_destinations(edge["target"])
                for edge in branch_edges
            ]
            if any(
                left
                and right
                and left.isdisjoint(right)
                for index, left in enumerate(destinations_by_branch)
                for right in destinations_by_branch[index + 1:]
            ):
                generic_topology_satisfied = True
                break

    topology_satisfied = (
        condition_topology_satisfied
        if contract["two_condition_branches"]
        else generic_topology_satisfied
    )
    if contract["distinct_destinations"] and not topology_satisfied:
        errors.append(
            "The requested branches do not reach two distinct terminal Destinations."
        )
    for label in sorted(bypass_labels):
        errors.append(
            f"Branch target {label!r} also has an upstream bypass connection."
        )
    return errors

def _build_graph_sync_guardrail(
    before_graph: dict | None,
    after_graph: dict | None,
    user_message: str,
    fetch_error: str | None = None,
    repaired: bool = False,
) -> dict:
    before_nodes, before_edges = _graph_counts(before_graph)
    after_nodes, after_edges = _graph_counts(after_graph)
    graph_changed = _graph_signature(before_graph) != _graph_signature(after_graph)
    updated_at = after_graph.get("updated_at") if isinstance(after_graph, dict) else None
    validation = (
        validate_pipeline_graph(after_graph)
        if graph_changed and after_nodes > 0 and not fetch_error
        else {"valid": True, "issues": []}
    )
    validation_errors = validation_issue_messages(validation)
    topology_errors = (
        _requested_topology_errors(after_graph, user_message)
        if not fetch_error
        else []
    )
    validation_errors.extend(topology_errors)

    if fetch_error:
        return {
            "status": "degraded",
            "guardrail_passed": False,
            "graph_safe_to_apply": False,
            "graph_changed": graph_changed,
            "node_count": after_nodes,
            "edge_count": after_edges,
            "updated_at": updated_at,
            "message": f"Agent replied, but graph sync verification failed: {fetch_error}",
            "repaired": repaired,
            "validation_errors": validation_errors,
        }

    if (graph_changed and not validation["valid"]) or topology_errors:
        return {
            "status": "invalid",
            "guardrail_passed": False,
            "graph_safe_to_apply": False,
            "graph_changed": graph_changed,
            "node_count": after_nodes,
            "edge_count": after_edges,
            "updated_at": updated_at,
            "message": (
                (
                    "The agent changed the graph, but the persisted result failed "
                    "pipeline validation: "
                    if graph_changed
                    else "The agent did not complete the requested pipeline topology: "
                )
                + "; ".join(validation_errors)
            ),
            "repaired": repaired,
            "validation_errors": validation_errors,
        }

    if graph_changed:
        return {
            "status": "synced",
            "guardrail_passed": True,
            "graph_safe_to_apply": True,
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
        " If an existing configured Subpipeline reports a missing required input, keep its "
        "saved reference and repair the parent wiring: insert or connect an upstream Source "
        "to that public input, then connect its output to downstream processing and a terminal "
        "Destination. Do not recreate or repeatedly reconfigure the reusable pipeline."
        " Connection port ids never include a component type or label prefix. Use exact "
        "local ids such as data, input, output, value, when_true, when_false, items, or "
        "item. Call overview to inspect custom or Subpipeline port ids. Never use forms "
        "such as source.data, task.input, task.output, or destination.data."
        + (f"\n\nVALIDATION ERRORS:\n{error_context}" if error_context else "")
        + "\n\n"
        + _build_agent_task(user_message, canvas_graph, backend_graph)
    )
