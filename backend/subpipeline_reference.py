from __future__ import annotations

import json
import re
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
    return str(node.get("id") or _node_data(node).get("flow_id") or "").strip()


def _public_id(value: Any, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", str(value or "").strip().lower())
    return normalized.strip("-") or fallback


def missing_explicit_port_contracts(graph: Any) -> list[str]:
    """Return component ids whose reusable-pipeline contract was left implicit."""
    nodes = graph.get("nodes") if isinstance(graph, dict) and isinstance(graph.get("nodes"), list) else []
    missing: list[str] = []
    for index, node in enumerate(nodes, start=1):
        data = _node_data(node)
        ports = data.get("ports") or data.get("ports_json")
        if isinstance(ports, str):
            try:
                ports = json.loads(ports)
            except (TypeError, ValueError):
                ports = None
        valid = isinstance(ports, dict) and all(
            isinstance(ports.get(direction), list)
            for direction in ("inputs", "outputs")
        )
        if valid:
            declared = [
                port
                for direction in ("inputs", "outputs")
                for port in ports[direction]
            ]
            valid = all(
                isinstance(port, dict)
                and str(port.get("id") or "").strip()
                and str(port.get("type") or "").strip().lower() not in {"", "any", "unknown", "*"}
                for port in declared
            )
        if not valid:
            missing.append(_node_id(node) or f"component-{index}")
    return missing


def normalize_reusable_pipeline_graph(graph: Any) -> dict[str, Any]:
    """Normalize React Flow graphs and compact agent snapshots to one UI shape."""
    candidate = graph if isinstance(graph, dict) else {}
    raw_nodes = candidate.get("nodes") if isinstance(candidate.get("nodes"), list) else []
    nodes: list[dict[str, Any]] = []
    ports_by_node: dict[str, dict[str, list[dict[str, Any]]]] = {}

    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            continue
        node_id = _node_id(raw)
        if not node_id:
            continue
        has_nested_data = isinstance(raw.get("data"), dict)
        data = dict(_node_data(raw))
        kind = normalize_step_type(data.get("type"), default="task")
        template_label = str(
            data.get("template_label")
            or (data.get("template") if isinstance(data.get("template"), str) else "")
            or ""
        ).strip()
        ports = ports_for_template(
            data.get("ports") or data.get("ports_json"),
            kind,
            template_label,
        )
        position = raw.get("position") if isinstance(raw.get("position"), dict) else {}
        try:
            x = float(position.get("x", index * 280))
        except (TypeError, ValueError):
            x = float(index * 280)
        try:
            y = float(position.get("y", 120))
        except (TypeError, ValueError):
            y = 120.0
        node_data = {
            **data,
            "type": kind,
            "label": str(data.get("label") or ""),
            "description": str(data.get("description") or ""),
            "ports": ports,
        }
        node_data.pop("id", None)
        node_data.pop("position", None)
        node_data.pop("template", None)
        if template_label:
            node_data["template_label"] = template_label
        nodes.append({
            "id": node_id,
            "type": str(raw.get("type") or "custom") if has_nested_data else "custom",
            "position": {"x": x, "y": y},
            "data": node_data,
        })
        ports_by_node[node_id] = ports

    node_ids = set(ports_by_node)
    raw_edges = candidate.get("edges") if isinstance(candidate.get("edges"), list) else []
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in raw_edges:
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source") or "").strip()
        target = str(raw.get("target") or "").strip()
        if not source or not target or source == target or source not in node_ids or target not in node_ids:
            continue
        source_outputs = ports_by_node[source]["outputs"]
        target_inputs = ports_by_node[target]["inputs"]
        source_handle = str(raw.get("sourceHandle") or raw.get("source_port") or "").strip()
        target_handle = str(raw.get("targetHandle") or raw.get("target_port") or "").strip()
        if source_handle == "output" and not any(port["id"] == source_handle for port in source_outputs):
            source_handle = ""
        if target_handle == "input" and not any(port["id"] == target_handle for port in target_inputs):
            target_handle = ""
        source_handle = source_handle or (str(source_outputs[0]["id"]) if source_outputs else "")
        target_handle = target_handle or (str(target_inputs[0]["id"]) if target_inputs else "")
        edge_key = (source, source_handle, target, target_handle)
        if edge_key in seen:
            continue
        seen.add(edge_key)
        edges.append({
            **raw,
            "id": str(raw.get("id") or f"e-{source}-{source_handle or 'default'}-{target}-{target_handle or 'default'}"),
            "source": source,
            "target": target,
            "sourceHandle": source_handle or None,
            "targetHandle": target_handle or None,
        })

    normalized = {
        "nodes": nodes,
        "edges": edges,
    }
    if "updated_at" in candidate:
        normalized["updated_at"] = candidate.get("updated_at")
    if isinstance(candidate.get("settings"), dict):
        normalized["settings"] = candidate["settings"]
    return normalized


def derive_subpipeline_interface(graph: Any) -> dict[str, list[dict[str, Any]]]:
    """Derive a reusable pipeline's public contract from boundary components."""
    nodes = graph.get("nodes") if isinstance(graph, dict) and isinstance(graph.get("nodes"), list) else []

    def bindings(
        boundary_kind: str,
        boundary_port_direction: str,
    ) -> list[dict[str, Any]]:
        entries: list[tuple[str, str, dict[str, Any]]] = []
        for node in nodes:
            data = _node_data(node)
            kind = normalize_step_type(data.get("type"), default="task")
            if kind != boundary_kind:
                continue
            node_id = _node_id(node)
            ports = ports_for_template(
                data.get("ports") or data.get("ports_json"),
                kind,
                data.get("template_label"),
            )
            for port in ports[boundary_port_direction]:
                entries.append((node_id, str(data.get("label") or node_id), port))

        counts: dict[str, int] = {}
        for _node, _label, port in entries:
            port_id = str(port.get("id") or "")
            counts[port_id] = counts.get(port_id, 0) + 1

        result: list[dict[str, Any]] = []
        for index, (node_id, node_label, port) in enumerate(entries, start=1):
            internal_port_id = str(port.get("id") or f"port-{index}")
            public_id = internal_port_id
            if counts.get(internal_port_id, 0) > 1:
                public_id = f"{_public_id(node_label, node_id)}.{internal_port_id}"
            result.append({
                **dict(port),
                "id": public_id,
                "internal": {"node": node_id, "port": internal_port_id},
            })
        return result

    return {
        "inputs": bindings("source", "outputs"),
        "outputs": bindings("destination", "inputs"),
    }


def public_ports_for_interface(interface: Any) -> dict[str, list[dict[str, Any]]]:
    candidate = interface if isinstance(interface, dict) else {}

    def ports(direction: str) -> list[dict[str, Any]]:
        raw = candidate.get(direction)
        if not isinstance(raw, list):
            return []
        return [
            {key: value for key, value in item.items() if key != "internal"}
            for item in raw
            if isinstance(item, dict)
        ]

    return {"inputs": ports("inputs"), "outputs": ports("outputs")}


def plan_subpipeline_port_migration(
    previous_ports: Any,
    next_ports: Any,
    connected_inputs: list[str],
    connected_outputs: list[str],
    *,
    requested_inputs: Any = None,
    requested_outputs: Any = None,
) -> dict[str, Any]:
    """Plan a safe contract migration using ids first, then unambiguous semantics."""
    previous = previous_ports if isinstance(previous_ports, dict) else {}
    following = next_ports if isinstance(next_ports, dict) else {}

    def compatible(left: Any, right: Any) -> bool:
        left_type = str(left or "").strip().lower()
        right_type = str(right or "").strip().lower()
        wildcard = {"", "any", "unknown", "*"}
        return left_type in wildcard or right_type in wildcard or left_type == right_type

    def direction_plan(
        direction: str,
        connected: list[str],
        requested: Any,
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        old_ports = [port for port in previous.get(direction, []) if isinstance(port, dict)]
        new_ports = [port for port in following.get(direction, []) if isinstance(port, dict)]
        old_by_id = {str(port.get("id") or ""): port for port in old_ports}
        new_by_id = {str(port.get("id") or ""): port for port in new_ports}
        requested_map = requested if isinstance(requested, dict) else {}
        mapping: dict[str, str] = {}
        conflicts: list[dict[str, Any]] = []

        for old_id in dict.fromkeys(str(value or "").strip() for value in connected):
            if not old_id:
                continue
            old_port = old_by_id.get(old_id, {"id": old_id, "name": old_id, "type": "any"})
            requested_id = str(requested_map.get(old_id) or "").strip()
            if requested_id:
                target = new_by_id.get(requested_id)
                if target and compatible(old_port.get("type"), target.get("type")):
                    mapping[old_id] = requested_id
                    continue
                conflicts.append({
                    "direction": direction,
                    "port": old_id,
                    "reason": "The requested target port is missing or type-incompatible.",
                    "candidates": list(new_by_id),
                })
                continue

            same_id = new_by_id.get(old_id)
            if same_id and compatible(old_port.get("type"), same_id.get("type")):
                mapping[old_id] = old_id
                continue

            old_name = str(old_port.get("name") or old_id).strip().lower()
            semantic_matches = [
                port for port in new_ports
                if str(port.get("name") or port.get("id") or "").strip().lower() == old_name
                and compatible(old_port.get("type"), port.get("type"))
            ]
            if len(semantic_matches) == 1:
                mapping[old_id] = str(semantic_matches[0].get("id") or "")
                continue

            compatible_targets = [
                port for port in new_ports
                if compatible(old_port.get("type"), port.get("type"))
            ]
            if len(compatible_targets) == 1 and len(set(connected)) == 1:
                mapping[old_id] = str(compatible_targets[0].get("id") or "")
                continue

            conflicts.append({
                "direction": direction,
                "port": old_id,
                "reason": "No unambiguous compatible target port exists.",
                "candidates": [
                    {
                        "id": str(port.get("id") or ""),
                        "name": str(port.get("name") or port.get("id") or ""),
                        "type": str(port.get("type") or "any"),
                    }
                    for port in compatible_targets
                ],
            })
        return mapping, conflicts

    input_mapping, input_conflicts = direction_plan("inputs", connected_inputs, requested_inputs)
    output_mapping, output_conflicts = direction_plan("outputs", connected_outputs, requested_outputs)
    conflicts = input_conflicts + output_conflicts
    return {
        "compatible": not conflicts,
        "input_mapping": input_mapping,
        "output_mapping": output_mapping,
        "conflicts": conflicts,
    }


def subpipeline_reference(value: Any) -> dict[str, str]:
    candidate = value if isinstance(value, dict) else {}
    reference = candidate.get("reference")
    reference = reference if isinstance(reference, dict) else {}
    return {
        "pipeline_uid": str(reference.get("pipeline_uid") or "").strip(),
        "pipeline_name": str(reference.get("pipeline_name") or "").strip(),
        "version_uid": str(reference.get("version_uid") or "").strip(),
        "version_name": str(reference.get("version_name") or "").strip(),
    }


def persisted_subpipeline_definition(value: Any) -> dict[str, Any]:
    """Persist only a reference for v2 nodes while keeping v1 graphs migratable."""
    candidate = dict(value) if isinstance(value, dict) else {}
    candidate.pop("resolved_graph", None)
    reference = subpipeline_reference(candidate)
    if reference["pipeline_uid"] and reference["version_uid"]:
        candidate.pop("graph", None)
        candidate["version"] = 2
    elif isinstance(candidate.get("graph"), dict):
        # A legacy embedded graph is retained only so the inspector can convert it
        # into a separately saved reusable pipeline without losing user work.
        candidate["version"] = 1
    else:
        candidate["version"] = 2
    return candidate
