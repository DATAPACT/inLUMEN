"""Scenarios that every GraphStore must handle the same way.

Each scenario drives the agent's tools (not the store directly) through a
sequence of calls and records what the model would see, plus the final graph.
:func:`observe` returns those observations with generated ids and timestamps
masked, so two stores can be compared for equality:

    observed = await observe(InMemoryGraphStore(), "branching")

The backend's Neo4j parity test runs every scenario on both stores.
"""
from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from typing import Any

from .store import GraphStore
from .tools import build_pipeline_editor_tools

Step = tuple[str, dict[str, Any] | Callable[[list[Any]], dict[str, Any]]]

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UUID_IN_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_VOLATILE_KEYS = {"created_at", "updated_at", "pipeline_updated_at", "exported_at", "added_at"}
# Where attachments are stored is up to the store (the backend uses per-workspace buckets).
_STORAGE_KEYS = {"bucket"}

_REUSABLE_GRAPH = {
    "nodes": [
        {"id": "audio", "position": {"x": 0, "y": 0}, "data": {
            "type": "source", "label": "Audio",
            "ports": {"inputs": [], "outputs": [{"id": "audio", "name": "audio", "type": "audio"}]}}},
        {"id": "transcribe", "position": {"x": 300, "y": 0}, "data": {
            "type": "task", "label": "Transcribe", "description": "Speech to text."}},
        {"id": "text", "position": {"x": 600, "y": 0}, "data": {
            "type": "destination", "label": "Transcript",
            "ports": {"inputs": [{"id": "transcript", "name": "transcript", "type": "text"}], "outputs": []}}},
    ],
    "edges": [
        {"source": "audio", "target": "transcribe", "sourceHandle": "audio", "targetHandle": "input"},
        {"source": "transcribe", "target": "text", "sourceHandle": "output", "targetHandle": "transcript"},
    ],
}


def _uid_of(label: str) -> Callable[[list[Any]], dict[str, Any]]:
    """Params for delete_step: the uid of the step labelled ``label`` in the last overview."""

    def params(outputs: list[Any]) -> dict[str, Any]:
        for output in reversed(outputs):
            if isinstance(output, list):
                for row in output:
                    step = row.get("step") if isinstance(row, dict) else None
                    if isinstance(step, dict) and step.get("label") == label:
                        return {"step_uid": step["uid"]}
        raise AssertionError(f"no step labelled {label!r} in an earlier overview")

    return params


def _reusable_uid(outputs: list[Any]) -> str:
    for output in reversed(outputs):
        if isinstance(output, list):
            for row in output:
                reusable = row.get("reusable_pipeline") if isinstance(row, dict) else None
                if isinstance(reusable, dict) and reusable.get("pipeline_uid"):
                    return reusable["pipeline_uid"]
    raise AssertionError("no reusable pipeline in an earlier result")


_SEED_GRAPH = {
    "nodes": [
        {"id": "1", "position": {"x": 0, "y": 0}, "data": {
            "type": "source", "label": "Tickets", "template_label": "Custom", "files": ["tickets.csv"]}},
        {"id": "2", "position": {"x": 300, "y": 0}, "data": {
            "type": "flow", "label": "Urgent?", "template_label": "Condition",
            "param": {"expression": "value.priority == 'high'"}}},
        {"id": "3", "position": {"x": 600, "y": -180}, "data": {
            "type": "destination", "label": "Escalate", "template_label": "Custom"}},
        {"id": "7", "position": {"x": 600, "y": 180}, "data": {
            "type": "destination", "label": "Queue", "template_label": "Custom"}},
    ],
    "edges": [
        {"source": "1", "target": "2", "sourceHandle": "data", "targetHandle": "value"},
        {"source": "2", "target": "3", "sourceHandle": "when_true", "targetHandle": "data"},
        {"source": "2", "target": "7", "sourceHandle": "when_false", "targetHandle": "data"},
    ],
}

# A step named RESTORE puts this graph back through GraphStore.restore_graph.
RESTORE = "__restore_graph__"

SCENARIOS: dict[str, list[Step]] = {
    "linear": [
        ("create_pipeline", {"name": "Invoice's report", "description": "Totals per vendor", "version": ""}),
        ("create_step", {"type": "source", "label": "Invoices", "description": "Invoice PDFs"}),
        ("create_step", {"type": "task", "label": "Extract totals", "description": "Read each total"}),
        ("create_step", {"type": "destination", "label": "Report", "description": "CSV report"}),
        ("create_pipeline", {"name": "", "description": "Updated", "version": "Draft"}),
        ("overview", {}),
    ],
    "branching": [
        ("create_step", {"type": "source", "label": "Orders", "description": "Order table"}),
        ("create_step", {"type": "flow", "label": "Valid?", "description": "Route valid orders", "template": "Condition"}),
        ("configure_flow_step", {"flow_id": "2", "behavior": "Condition", "expression": "value.is_valid == true"}),
        ("create_step", {"type": "task", "label": "Price", "description": "Price valid orders",
                         "after_flow_id": "2", "source_port": "when_true"}),
        ("create_step", {"type": "destination", "label": "Rejected", "description": "Invalid orders",
                         "after_flow_id": "2", "source_port": "when_false"}),
        ("create_step", {"type": "destination", "label": "Priced", "description": "Priced orders",
                         "after_flow_id": "3"}),
        ("connect_steps", {"source_flow_id": "2", "target_flow_id": "4", "source_port": "when_false"}),
        ("connect_steps", {"source_flow_id": "3", "target_flow_id": "4"}),
        ("connect_steps", {"source_flow_id": "3", "target_flow_id": "4", "allow_fan_out": True}),
        ("disconnect_steps", {"source_flow_id": "3", "target_flow_id": "4",
                              "source_port": "output", "target_port": "data"}),
        ("disconnect_steps", {"source_flow_id": "3", "target_flow_id": "5",
                              "source_port": "wrong", "target_port": "data"}),
        ("overview", {}),
    ],
    "join": [
        ("create_step", {"type": "source", "label": "Customers", "description": "Customer table"}),
        ("create_step", {"type": "source", "label": "Orders", "description": "Order table"}),
        ("create_step", {"type": "task", "label": "No tail", "description": "Two tails: rejected"}),
        ("create_step", {"type": "task", "label": "Join", "description": "Join orders to customers",
                         "inputs": ["customers", "orders"], "after_flow_id": "1", "target_port": "customers"}),
        ("connect_steps", {"source_flow_id": "2", "target_flow_id": "3", "target_port": "orders"}),
        ("create_step", {"type": "destination", "label": "Joined", "description": "Joined table"}),
        ("create_step", {"type": "source", "label": "Late", "description": "x", "after_flow_id": "1"}),
        ("overview", {}),
    ],
    "map_collect": [
        ("create_step", {"type": "source", "label": "PDFs", "description": "Invoice PDFs"}),
        ("create_step", {"type": "flow", "label": "For each invoice", "description": "One invoice at a time"}),
        ("create_step", {"type": "task", "label": "Extract", "description": "Extract fields"}),
        ("create_step", {"type": "flow", "label": "Collect invoices", "description": "Gather results"}),
        ("create_step", {"type": "destination", "label": "Table", "description": "One table"}),
        ("configure_flow_step", {"flow_id": "2", "behavior": "Condition", "expression": "value.total > 0"}),
        ("configure_flow_step", {"flow_id": "2", "behavior": "Parallel Map"}),
        ("configure_flow_step", {"flow_id": "3", "behavior": "Collect"}),
        ("overview", {}),
    ],
    "insert_delete": [
        ("create_step", {"type": "source", "label": "Input", "description": "Raw records"}),
        ("create_step", {"type": "task", "label": "Score", "description": "Score records"}),
        ("create_step", {"type": "destination", "label": "Output", "description": "Scores"}),
        ("insert_step", {"type": "task", "label": "Clean", "description": "Drop bad rows",
                         "before_flow_id": "2", "after_flow_id": "1"}),
        ("insert_step", {"type": "task", "label": "Nope", "description": "Not connected",
                         "before_flow_id": "3", "after_flow_id": "1"}),
        ("insert_step", {"type": "source", "label": "Seed", "description": "Initial",
                         "before_flow_id": "1"}),
        ("insert_step", {"type": "source", "label": "Seed 2", "description": "Initial",
                         "before_flow_id": "2"}),
        ("overview", {}),
        ("delete_step", _uid_of("Score")),
        ("overview", {}),
        ("delete_step", _uid_of("Seed")),
        ("delete_step", {"step_uid": "missing-uid"}),
        ("overview", {}),
        ("delete_all_steps", {}),
        ("overview", {}),
    ],
    "restore": [
        (RESTORE, _SEED_GRAPH),
        ("overview", {}),
        ("insert_step", {"type": "task", "label": "Summarize", "description": "Short summary",
                         "before_flow_id": "3", "after_flow_id": "2"}),
        ("create_step", {"type": "task", "label": "Tag", "description": "Tag queue items",
                         "after_flow_id": "2", "source_port": "when_false"}),
        (RESTORE, _SEED_GRAPH),
        ("overview", {}),
    ],
    "reusable": [
        ("list_reusable_pipelines", {}),
        ("create_reusable_pipeline", {"name": "Transcription", "description": "Audio to text",
                                      "graph": _REUSABLE_GRAPH}),
        ("create_reusable_pipeline", {"name": "transcription ", "description": "Duplicate",
                                      "graph": _REUSABLE_GRAPH}),
        ("list_reusable_pipelines", {}),
        ("create_step", {"type": "source", "label": "Calls", "description": "Call recordings"}),
        ("create_step", {"type": "subpipeline", "label": "Transcribe calls", "description": "Reuse",
                         "reusable_pipeline_name": "Transcription"}),
        ("create_step", {"type": "destination", "label": "Transcripts", "description": "Text"}),
        ("configure_subpipeline_step", lambda outputs: {"flow_id": "2", "pipeline_uid": _reusable_uid(outputs)}),
        ("create_step", {"type": "subpipeline", "label": "Unknown", "description": "x",
                         "reusable_pipeline_name": "No such pipeline", "after_flow_id": "3"}),
        ("overview", {}),
    ],
}


async def run_scenario(store: GraphStore, name: str) -> tuple[list[Any], dict]:
    """Run scenario ``name`` on ``store``: each tool's decoded output, and the final graph."""
    tools = {tool.__name__: tool for tool in build_pipeline_editor_tools(store)}
    outputs: list[Any] = []
    for tool_name, params in SCENARIOS[name]:
        payload = params(outputs) if callable(params) else params
        if tool_name == RESTORE:
            await store.restore_graph(payload)
            outputs.append({"restored": True})
            continue
        try:
            text = await (tools[tool_name]() if tool_name == "overview" else tools[tool_name](json.dumps(payload)))
            outputs.append(_decode(text))
        except Exception as exc:
            outputs.append({"error": f"{type(exc).__name__}: {exc}"})
    return outputs, await store.get_graph()


def _decode(text: str) -> Any:
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        value = text
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<time>" if key in _VOLATILE_KEYS and item is not None
                else "<storage>" if key in _STORAGE_KEYS
                else _mask(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask(item) for item in value]
    if isinstance(value, str):
        return "<uid>" if _UUID.match(value) else _UUID_IN_TEXT.sub("<uid>", value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _sorted_rows(output: Any) -> Any:
    if isinstance(output, list):
        return sorted(output, key=lambda row: json.dumps(row, sort_keys=True))
    return output


def _graph_view(graph: dict) -> dict:
    nodes = sorted(graph.get("nodes") or [], key=lambda node: str(node.get("id")))
    edges = sorted(graph.get("edges") or [], key=lambda edge: str(edge.get("id")))
    pipeline = graph.get("pipeline") or {}
    return {
        "pipeline": {key: pipeline.get(key) for key in ("name", "label", "description", "version", "status")},
        "nodes": nodes,
        "edges": edges,
    }


async def observe(store: GraphStore, name: str) -> dict[str, Any]:
    """Scenario observations with ids and timestamps masked (comparable across stores)."""
    outputs, graph = await run_scenario(store, name)
    masked = _mask({"outputs": outputs, "graph": _graph_view(graph)})
    masked["outputs"] = [_sorted_rows(output) for output in masked["outputs"]]
    return masked
