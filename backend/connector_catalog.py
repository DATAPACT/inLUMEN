"""Authoritative registry for platform-owned pipeline boundary connectors."""

from __future__ import annotations

from typing import Any


CONNECTOR_DEFINITIONS: dict[str, dict[str, dict[str, Any]]] = {
    "source": {
        "custom": {"required": ()},
        "file": {"required": ()},
        "folder": {"required": ()},
        "user upload": {"required": ()},
        "database": {"required": ("connection_url", "query")},
        "object storage": {"required": ("bucket",)},
        "rest api": {"required": ("url",)},
    },
    "destination": {
        "custom": {"required": ()},
        "file": {"required": ("filename",)},
        "folder": {"required": ()},
        # Read-only compatibility aliases for existing v1 pipelines. New
        # designs use File or Folder from the palette.
        "file output": {"required": ()},
        "folder output": {"required": ()},
        "json output": {"required": ()},
        "structured json": {"required": ()},
        "structured object": {"required": ()},
        "object storage": {"required": ("bucket",)},
        "rest api": {"required": ("url",)},
    },
}


def normalize_connector_template(value: object) -> str:
    return " ".join(str(value or "custom").strip().lower().split()) or "custom"


def connector_definition(step_type: object, template: object) -> dict[str, Any] | None:
    kind = str(step_type or "").strip().lower()
    return CONNECTOR_DEFINITIONS.get(kind, {}).get(normalize_connector_template(template))


def require_supported_connector(step_type: object, template: object) -> None:
    kind = str(step_type or "").strip().lower()
    if kind not in CONNECTOR_DEFINITIONS:
        return
    connector = normalize_connector_template(template)
    if connector in CONNECTOR_DEFINITIONS[kind]:
        return
    available = ", ".join(
        name.title() for name in sorted(CONNECTOR_DEFINITIONS[kind])
    )
    raise ValueError(
        f"{template or 'Custom'} is not a registered managed {kind} connector. "
        f"Available connectors: {available}."
    )


def missing_connector_parameters(
    step_type: object,
    template: object,
    parameters: Any,
) -> list[str]:
    definition = connector_definition(step_type, template) or {}
    values = parameters if isinstance(parameters, dict) else {}
    return [
        name
        for name in definition.get("required", ())
        if values.get(name) is None or not str(values.get(name)).strip()
    ]
