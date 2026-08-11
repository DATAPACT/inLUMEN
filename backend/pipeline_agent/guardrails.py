"""Validation and repair policy for pipeline-agent graph mutations."""

from pipeline_agent.context import (
    _build_agent_task,
    _graph_counts,
    _graph_signature,
)
from pipeline_graph_validation import (
    validate_pipeline_graph,
    validation_issue_messages,
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
    graph_changed = _graph_signature(before_graph) != _graph_signature(after_graph)
    updated_at = after_graph.get("updated_at") if isinstance(after_graph, dict) else None
    validation = (
        validate_pipeline_graph(after_graph)
        if graph_changed and after_nodes > 0 and not fetch_error
        else {"valid": True, "issues": []}
    )
    validation_errors = validation_issue_messages(validation)

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

    if graph_changed and not validation["valid"]:
        return {
            "status": "invalid",
            "guardrail_passed": False,
            "graph_safe_to_apply": False,
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
        + (f"\n\nVALIDATION ERRORS:\n{error_context}" if error_context else "")
        + "\n\n"
        + _build_agent_task(user_message, canvas_graph, backend_graph)
    )
