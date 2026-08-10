from __future__ import annotations

import json
import re
from typing import Any

from step_types import normalize_step_type


DEFAULT_NODE_PORTS = {
    "source": {"inputs": [], "outputs": [{"id": "data", "name": "data", "type": "any", "required": True, "description": "Source data."}]},
    "task": {
        "inputs": [{"id": "input", "name": "input", "type": "any", "required": True, "description": "Task input."}],
        "outputs": [{"id": "output", "name": "output", "type": "any", "required": True, "description": "Task output."}],
    },
    "destination": {"inputs": [{"id": "data", "name": "data", "type": "any", "required": True, "description": "Data to deliver."}], "outputs": []},
    "flow": {
        "inputs": [{"id": "input", "name": "input", "type": "any", "required": True, "description": "Flow input."}],
        "outputs": [{"id": "output", "name": "output", "type": "any", "required": True, "description": "Flow output."}],
    },
    "subpipeline": {
        "inputs": [{"id": "input", "name": "input", "type": "any", "required": True, "description": "Nested pipeline input."}],
        "outputs": [{"id": "output", "name": "output", "type": "any", "required": True, "description": "Nested pipeline output."}],
    },
}


def _copy_ports(ports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(port) for port in ports]


def _port_id(value: Any, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", str(value or "").strip().lower())
    return normalized.strip("-") or fallback


def _normalize_port_list(value: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return _copy_ports(fallback)
    normalized: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("label") or entry.get("id") or "").strip()
        base_id = _port_id(entry.get("id") or name, f"port-{index}")
        port_id = base_id
        suffix = 2
        while port_id in used:
            port_id = f"{base_id}-{suffix}"
            suffix += 1
        used.add(port_id)
        port = {
            "id": port_id,
            "name": name or port_id,
            "type": str(entry.get("type") or entry.get("data_type") or "any").strip() or "any",
            "required": entry.get("required") if isinstance(entry.get("required"), bool) else True,
            "description": str(entry.get("description") or "").strip(),
        }
        port_format = str(entry.get("format") or "").strip()
        if port_format:
            port["format"] = port_format
        if isinstance(entry.get("schema"), dict):
            port["schema"] = dict(entry["schema"])
        normalized.append(port)
    return normalized


def normalize_node_ports(value: Any, step_type: object) -> dict[str, list[dict[str, Any]]]:
    kind = normalize_step_type(step_type)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    candidate = value if isinstance(value, dict) else {}
    defaults = DEFAULT_NODE_PORTS[kind]
    return {
        "inputs": []
        if kind == "source"
        else _normalize_port_list(candidate.get("inputs"), defaults["inputs"]),
        "outputs": []
        if kind == "destination"
        else _normalize_port_list(candidate.get("outputs"), defaults["outputs"]),
    }


def ports_json(value: Any, step_type: object) -> str:
    return json.dumps(
        normalize_node_ports(value, step_type),
        ensure_ascii=False,
        sort_keys=True,
    )


def default_input_port_id(step_type: object) -> str:
    inputs = DEFAULT_NODE_PORTS[normalize_step_type(step_type)]["inputs"]
    return inputs[0]["id"] if inputs else ""


def default_output_port_id(step_type: object) -> str:
    outputs = DEFAULT_NODE_PORTS[normalize_step_type(step_type)]["outputs"]
    return outputs[0]["id"] if outputs else ""
