"""Plugins: third-party tools with a deployed REST API, used as pipeline steps.

A plugin is a manifest that bridges inLUMEN to a tool already deployed behind a
reachable REST API. Plugin-backed STEPs store ``template_label = "plugin:<id>"``.
"""
from __future__ import annotations

from typing import Any

from .manifest import (
    PLUGIN_TEMPLATE_PREFIX,
    PluginManifestError,
    is_plugin_template,
    plugin_id_from_template,
    plugin_ports,
    plugin_summary,
    plugin_template,
    required_parameter_names,
    secret_parameter_names,
    validate_plugin_manifest,
)

__all__ = [
    "PLUGIN_TEMPLATE_PREFIX",
    "PluginManifestError",
    "PluginNotInstalled",
    "installed_plugin_manifest",
    "is_plugin_template",
    "plugin_id_from_template",
    "plugin_manifest_for_template",
    "plugin_ports",
    "plugin_summary",
    "plugin_template",
    "required_parameter_names",
    "secret_parameter_names",
    "validate_plugin_manifest",
    "with_plugin_secret_keys",
]


class PluginNotInstalled(ValueError):
    pass


def installed_plugin_manifest(plugin_id: str) -> dict[str, Any] | None:
    """Return the pinned manifest of an installed plugin (indirection for tests)."""
    from .store import get_installed_manifest

    return get_installed_manifest(plugin_id)


def plugin_manifest_for_template(template: object, *, required: bool = True) -> dict[str, Any] | None:
    plugin_id = plugin_id_from_template(template)
    if plugin_id is None:
        return None
    manifest = installed_plugin_manifest(plugin_id)
    if manifest is None and required:
        raise PluginNotInstalled(
            f"Plugin {plugin_id!r} is not installed. Ask an administrator to add its "
            "definition, or choose another tool."
        )
    return manifest


def with_plugin_secret_keys(secret_keys: Any, template: object, parameters: Any) -> Any:
    """Force a plugin's declared secrets to be treated as secret parameters.

    Defence in depth: even if a client forgets to flag a plugin credential as
    secret, its value is never persisted in the graph or deployment bundles.
    Returns ``secret_keys`` unchanged for non-plugin steps.
    """
    if not is_plugin_template(template):
        return secret_keys
    try:
        manifest = plugin_manifest_for_template(template, required=False)
    except Exception:  # registry unavailable: keep the caller's behaviour
        manifest = None
    from node_parameters import is_sensitive_parameter_name, normalize_secret_param_keys

    params = parameters if isinstance(parameters, dict) else {}
    keys = normalize_secret_param_keys(secret_keys, params)
    if not manifest:
        # Fail closed when the manifest is unavailable (registry outage, plugin
        # not installed yet): anything that looks like a credential is secret.
        keys.extend(k for k in params if is_sensitive_parameter_name(k) and k not in keys)
        return keys
    for name in secret_parameter_names(manifest):
        if name in params and name not in keys:
            keys.append(name)
    return keys
