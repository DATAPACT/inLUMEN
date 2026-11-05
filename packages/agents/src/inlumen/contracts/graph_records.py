"""Stored pipeline records to the canvas graph (React Flow export shape).

A stored pipeline is a set of STEP property maps, the files attached to each
step and FLOWS_TO connections between steps. The inLUMEN backend reads them
from Neo4j; ``inlumen.design``'s in-memory store keeps them in Python. Both
build the graph the canvas and the design agent see with these functions.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from .node_definitions.instance import definition_data_from_properties
from .node_parameters import normalize_secret_param_keys
from .node_ports import default_input_port_id, default_output_port_id, normalize_node_ports
from .step_types import normalize_step_type
from .subpipeline_reference import (
    derive_subpipeline_interface,
    hydrate_reusable_file_references,
    normalize_reusable_pipeline_graph,
    reusable_pipeline_nesting_error,
)

# (pipeline_uid, legacy_version_uid or None) -> {"pipeline_name", "graph_json"} or None
ReusableLookup = Callable[[str, str | None], dict[str, Any] | None]


def reusable_graph(record: dict[str, Any]) -> dict:
    """The graph of a stored reusable pipeline record."""
    return hydrate_reusable_file_references(
        normalize_reusable_pipeline_graph(json.loads(record.get("graph_json") or "{}"))
    )


def step_node(
    props: dict[str, Any],
    files: list[dict[str, Any]] | None = None,
    *,
    resolve_reusable: ReusableLookup | None = None,
) -> dict[str, Any] | None:
    """One canvas node from a STEP's properties, or None without a flow_id."""
    flow_id = props.get("flow_id")
    if flow_id is None:
        return None

    node_id = str(flow_id)

    # position
    try:
        x = float(props.get("x", 0) or 0)
    except Exception:
        x = 0.0
    try:
        y = float(props.get("y", 0) or 0)
    except Exception:
        y = 0.0

    step_kind = normalize_step_type(props.get("type"), default="task")

    files_for_step = files or []
    # filenames list (simple)
    filenames = [
        f.get("filename")
        for f in files_for_step
        if isinstance(f, dict) and f.get("filename")
    ]

    data = {
        "label": props.get("label", ""),
        "description": props.get("description", ""),
        "type": step_kind,
        "template_label": props.get("template_label", ""),
        "ports": normalize_node_ports(props.get("ports_json"), step_kind),

        # add files so polling doesn't wipe them
        "files": filenames,

        # optional richer info (bucket + added_at)
        "file_buckets": files_for_step,
    }

    if step_kind == "destination" and "content" in props:
        data["content"] = props.get("content") or ""
    if "has_files" in props:
        data["has_files"] = props.get("has_files")
    if "endpoint" in props:
        data["endpoint"] = props.get("endpoint")
    if "database" in props:
        data["database"] = props.get("database")
    if "param_json" in props:
        param_json = props.get("param_json")
        data["param_json"] = param_json
        try:
            parsed_param = json.loads(param_json) if isinstance(param_json, str) else {}
        except Exception:
            parsed_param = {}
        data["param"] = parsed_param if isinstance(parsed_param, dict) else {}
        data["secret_params"] = normalize_secret_param_keys(
            props.get("secret_params_json"),
            data["param"],
        )
    data.update(definition_data_from_properties(props))
    if step_kind == "subpipeline" and isinstance(data.get("subpipeline"), dict):
        subpipeline = dict(data["subpipeline"])
        reference = subpipeline.get("reference")
        reference = reference if isinstance(reference, dict) else {}
        referenced_pipeline_uid = str(reference.get("pipeline_uid") or "").strip()
        if referenced_pipeline_uid:
            referenced = (
                resolve_reusable(referenced_pipeline_uid, reference.get("version_uid") or None)
                if resolve_reusable
                else None
            )
            subpipeline.pop("resolution_error", None)
            subpipeline.pop("resolved_graph", None)
            if referenced:
                resolved_graph = reusable_graph(referenced)
                subpipeline["reference"] = {**reference, "pipeline_uid": referenced_pipeline_uid, "pipeline_name": referenced['pipeline_name'] or ''}
                subpipeline["interface"] = derive_subpipeline_interface(resolved_graph)
                nesting_error = reusable_pipeline_nesting_error(resolved_graph)
                if nesting_error:
                    subpipeline["resolution_error"] = nesting_error
                else:
                    subpipeline["resolved_graph"] = resolved_graph
            else:
                subpipeline["resolution_error"] = "Referenced reusable pipeline was not found."
        data["subpipeline"] = subpipeline

    return {
        "id": node_id,
        "type": "custom",
        "position": {"x": x, "y": y},
        "data": data,
    }


def flow_edges(flows: Iterable[dict[str, Any]], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canvas edges from FLOWS_TO rows ``{source, target, source_port, target_port}``."""
    edges = []
    node_connection_profiles_by_id = {
        node["id"]: (
            node["data"]["type"],
            node["data"].get("template_label") or "",
        )
        for node in nodes
    }
    for f in flows:
        src = f.get("source") if isinstance(f, dict) else None
        tgt = f.get("target") if isinstance(f, dict) else None
        if src is None or tgt is None:
            continue
        src = str(src)
        tgt = str(tgt)
        source_profile = node_connection_profiles_by_id.get(src, ("task", ""))
        target_profile = node_connection_profiles_by_id.get(tgt, ("task", ""))
        source_port = f.get("source_port") or default_output_port_id(*source_profile)
        target_port = f.get("target_port") or default_input_port_id(*target_profile)
        edges.append({
            "id": (
                f"reactflow__edge-{src}-{source_port or 'default'}-"
                f"{tgt}-{target_port or 'default'}"
            ),
            "source": src,
            "target": tgt,
            "sourceHandle": source_port or None,
            "targetHandle": target_port or None,
        })
    return edges
