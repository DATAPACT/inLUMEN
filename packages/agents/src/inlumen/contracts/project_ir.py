"""inLUMEN Project JSON (``inlumen.project@2``) from a canvas graph.

Mirrors ``createProjectDocument`` in ``frontend/src/features/flow/projectIr.ts``,
so a document made here opens with the canvas Import button.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from .node_ports import normalize_node_ports
from .step_types import normalize_step_type

PROJECT_IR_SCHEMA_VERSION = "inlumen.project@2"

# The first template the palette lists for each component type.
_DEFAULT_TEMPLATES = {
    "source": "Custom",
    "task": "Blank Task",
    "destination": "Custom",
    "flow": "Condition",
    "subpipeline": "Subpipeline",
}


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _template(data: dict[str, Any], kind: str) -> dict[str, Any]:
    structured = _object(data.get("template"))
    name = str(structured.get("name") or data.get("template_label") or _DEFAULT_TEMPLATES[kind]).strip()
    name = name or _DEFAULT_TEMPLATES[kind]
    template_id = str(structured.get("id") or "custom." + re.sub(r"[^a-z0-9]+", "-", name.lower()))
    reference: dict[str, Any] = {"id": template_id, "name": name}
    version = structured.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        reference["version"] = version
    return reference


def _ir_node(node: dict[str, Any]) -> dict[str, Any]:
    data = _object(node.get("data"))
    kind = normalize_step_type(data.get("type"), default="task")
    ports = normalize_node_ports(data.get("ports"), kind)
    position = _object(node.get("position"))
    files = [item for item in data.get("files") or [] if item] if isinstance(data.get("files"), list) else []
    ir: dict[str, Any] = {
        "id": str(node.get("id")),
        "kind": kind,
        "template": _template(data, kind),
        "label": str(data.get("label") or ""),
        "description": str(data.get("description") or ""),
        "position": {"x": _number(position.get("x")), "y": _number(position.get("y"))},
        "inputs": ports["inputs"],
        "outputs": ports["outputs"],
        "parameters": _object(data.get("param") or data.get("parameters")),
    }
    if kind == "task":
        ir["implementation"] = dict(_object(data.get("implementation")))
    if files:
        ir["sample_data"] = files
    subpipeline = _object(data.get("subpipeline"))
    reference = _object(subpipeline.get("reference"))
    if kind == "subpipeline" and reference.get("pipeline_uid"):
        pinned = {
            "pipeline_uid": str(reference["pipeline_uid"]),
            "pipeline_name": str(reference.get("pipeline_name") or ""),
        }
        if reference.get("version_uid"):
            pinned["version_uid"] = str(reference["version_uid"])
            pinned["version_name"] = str(reference.get("version_name") or "")
        ir["subpipeline"] = {"version": 2, "reference": pinned, "interface": subpipeline.get("interface")}
    return ir


def project_document(graph: dict[str, Any], *, name: str = "", description: str = "") -> dict[str, Any]:
    """A Project JSON document for ``graph`` (nodes and edges in canvas shape)."""
    nodes = [node for node in graph.get("nodes") or [] if isinstance(node, dict)]
    edges = [edge for edge in graph.get("edges") or [] if isinstance(edge, dict)]
    return {
        "schema_version": PROJECT_IR_SCHEMA_VERSION,
        "project": {
            "name": name or "inLUMEN Project",
            "description": description,
            "exported_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        },
        "pipeline": {
            "nodes": [_ir_node(node) for node in nodes],
            "connections": [
                {
                    "id": str(edge.get("id") or f"{edge.get('source')}:{edge.get('sourceHandle')}->{edge.get('target')}:{edge.get('targetHandle')}"),
                    "from": {"node": str(edge.get("source")), "port": str(edge.get("sourceHandle") or "")},
                    "to": {"node": str(edge.get("target")), "port": str(edge.get("targetHandle") or "")},
                }
                for edge in edges
            ],
        },
    }
