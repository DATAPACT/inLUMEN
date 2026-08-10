from __future__ import annotations

import json
import re
from typing import Any

from step_types import normalize_step_type


DEFAULT_NODE_PORTS = {
    "source": {"inputs": [], "outputs": [{"id": "data", "label": "data"}]},
    "task": {
        "inputs": [{"id": "input", "label": "input"}],
        "outputs": [{"id": "output", "label": "output"}],
    },
    "sink": {"inputs": [{"id": "data", "label": "data"}], "outputs": []},
    "flow": {
        "inputs": [{"id": "input", "label": "input"}],
        "outputs": [{"id": "output", "label": "output"}],
    },
    "subpipeline": {
        "inputs": [{"id": "input", "label": "input"}],
        "outputs": [{"id": "output", "label": "output"}],
    },
}


def _copy_ports(ports: list[dict[str, str]]) -> list[dict[str, str]]:
    return [dict(port) for port in ports]


def _port_id(value: Any, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", str(value or "").strip().lower())
    return normalized.strip("-") or fallback


def _normalize_port_list(value: Any, fallback: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return _copy_ports(fallback)
    normalized: list[dict[str, str]] = []
    used: set[str] = set()
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("id") or "").strip()
        base_id = _port_id(entry.get("id") or label, f"port-{index}")
        port_id = base_id
        suffix = 2
        while port_id in used:
            port_id = f"{base_id}-{suffix}"
            suffix += 1
        used.add(port_id)
        port = {"id": port_id, "label": label or port_id}
        data_type = str(entry.get("data_type") or "").strip()
        if data_type:
            port["data_type"] = data_type
        normalized.append(port)
    return normalized


def normalize_node_ports(value: Any, step_type: object) -> dict[str, list[dict[str, str]]]:
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
        if kind == "sink"
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
