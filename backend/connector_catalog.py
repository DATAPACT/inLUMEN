"""Authoritative registry for platform-owned pipeline boundary connectors."""

from __future__ import annotations

from typing import Any


CONNECTOR_DEFINITIONS: dict[str, dict[str, dict[str, Any]]] = {
    "source": {
        "custom": {"required": (), "requires_attached_files": True},
        "file": {"required": (), "requires_attached_files": True},
        "folder": {"required": (), "requires_attached_files": True},
        "user upload": {"required": (), "requires_attached_files": True},
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


def _plugin_definition(kind: str, template: object, parameters: Any = None) -> dict[str, Any] | None:
    """Connector definition derived from an installed plugin manifest."""
    from plugins import plugin_manifest_for_template, required_parameter_names

    manifest = plugin_manifest_for_template(template)
    if manifest is None:
        return None
    if manifest["step_type"] != kind:
        raise ValueError(
            f"Plugin {manifest['name']!r} is a {manifest['step_type']} plugin and "
            f"cannot be used as a {kind}."
        )
    values = parameters if isinstance(parameters, dict) else {}
    return {
        "required": tuple(required_parameter_names(manifest, values.get("operation"))),
        "requires_attached_files": False,
        "plugin": manifest,
    }


def connector_definition(step_type: object, template: object) -> dict[str, Any] | None:
    kind = str(step_type or "").strip().lower()
    from plugins import is_plugin_template

    if is_plugin_template(template):
        try:
            return _plugin_definition(kind, template)
        except ValueError:
            return None
    return CONNECTOR_DEFINITIONS.get(kind, {}).get(normalize_connector_template(template))


def require_supported_connector(step_type: object, template: object) -> None:
    kind = str(step_type or "").strip().lower()
    from plugins import is_plugin_template

    if is_plugin_template(template):
        # Raises PluginNotInstalled / ValueError with an actionable message.
        _plugin_definition(kind, template)
        return
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
    from plugins import is_plugin_template

    if is_plugin_template(template):
        try:
            definition = _plugin_definition(
                str(step_type or "").strip().lower(), template, parameters
            ) or {}
        except ValueError:
            definition = {}
    else:
        definition = connector_definition(step_type, template) or {}
    values = parameters if isinstance(parameters, dict) else {}
    return [
        name
        for name in definition.get("required", ())
        if values.get(name) is None or not str(values.get(name)).strip()
    ]
