"""Build the deterministic runtime (main.py + contracts) for plugin-backed steps."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from node_parameters import is_sensitive_parameter_name, normalize_secret_param_keys

from .manifest import (
    parameters_for_operation,
    plugin_ports,
    resolve_operation,
    secret_parameter_names,
)

ADAPTER_TEMPLATE_PATH = Path(__file__).with_name("adapter_runtime.py")
_SPEC_LINE_MARKER = "# __PLUGIN_SPEC__"
ADAPTER_VALUE_NAMES = ("operation", "timeout", "output_filename")


def plugin_runtime_spec(step: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Pinned, secret-free specification embedded into the plugin adapter."""
    kind = str(step.get("type") or "").strip().lower()
    if kind != manifest["step_type"]:
        raise ValueError(
            f"Plugin {manifest['id']!r} is a {manifest['step_type']} plugin and cannot "
            f"be used on a {kind or 'unknown'} step."
        )
    raw = dict(step.get("param")) if isinstance(step.get("param"), dict) else {}
    secret_names = set(normalize_secret_param_keys(step.get("secret_params"), raw))
    secret_names.update(secret_parameter_names(manifest))
    operation = resolve_operation(manifest, raw.get("operation"))
    declarations = parameters_for_operation(manifest, operation["id"])
    allowed = {item["name"] for item in declarations} | set(ADAPTER_VALUE_NAMES)
    values = {
        key: value
        for key, value in raw.items()
        if key in allowed
        and key not in secret_names
        and not is_sensitive_parameter_name(key)
        and value not in (None, "")
    }
    values["operation"] = operation["id"]
    return {
        "plugin": {
            "id": manifest["id"],
            "version": manifest["version"],
            "name": manifest["name"],
            "publisher": manifest.get("publisher", ""),
        },
        "kind": kind,
        "base_url": manifest["endpoint"]["base_url"],
        "operation": operation,
        "parameters": declarations,
        "auth": manifest.get("auth") or {"type": "none"},
        "secrets": manifest.get("secrets") or [],
        "values": values,
    }


def plugin_main_source(spec: dict[str, Any]) -> str:
    template = ADAPTER_TEMPLATE_PATH.read_text(encoding="utf-8")
    lines = template.splitlines(keepends=True)
    embedded = json.dumps(json.dumps(spec, sort_keys=True))
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith("PLUGIN_SPEC = ") and _SPEC_LINE_MARKER in line:
            lines[index] = f"PLUGIN_SPEC = json.loads({embedded})\n"
            replaced = True
            break
    if not replaced:  # pragma: no cover - guards template edits
        raise RuntimeError("Plugin adapter template is missing the PLUGIN_SPEC marker line.")
    return "".join(lines)


def plugin_data_contract(spec: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    """Declared outputs, matching what the adapter writes to PIPELINE_OUTPUT_DIR."""
    if spec["kind"] == "destination":
        return {"contract_id": "inlumen.generic-node@2", "version": "2", "inputs": [], "outputs": []}
    response = spec["operation"].get("response") or {}
    output_name = str(spec["values"].get("output_filename") or response.get("filename"))
    directory = response.get("representation") == "directory"
    ports = step.get("ports") if isinstance(step.get("ports"), dict) else {}
    outputs = ports.get("outputs") or []
    port_id = str((outputs[0] if outputs and isinstance(outputs[0], dict) else {}).get("id") or "")
    declaration = {
        "name": Path(output_name).stem if not directory else output_name,
        "filename": output_name,
        "kind": "directory" if directory else ("json" if response.get("format") == "json" else "binary"),
        "format": response.get("format") or "bin",
        "representation": "directory" if directory else "file",
        "description": f"Response of {spec['plugin']['name']} operation {spec['operation']['id']}.",
    }
    if port_id:
        declaration["port"] = port_id
    return {
        "contract_id": "inlumen.generic-node@2",
        "version": "2",
        "inputs": [],
        "outputs": [declaration],
    }


def default_step_ports(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return plugin_ports(manifest)
