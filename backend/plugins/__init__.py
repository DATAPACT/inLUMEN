"""Plugins: third-party tools with a deployed REST API, used as pipeline steps.

Manifest handling and lookups live in ``inlumen.contracts.plugins``. This
package adds the backend's plugin store, routes and runtime, and registers the
store as the process-wide plugin source.
"""
from __future__ import annotations

from typing import Any

from inlumen.contracts.plugins import (  # noqa: F401  (backend-facing API)
    PLUGIN_TEMPLATE_PREFIX,
    CallablePluginSource,
    PluginManifestError,
    PluginNotInstalled,
    installed_plugin_manifest,
    is_plugin_template,
    plugin_id_from_template,
    plugin_manifest_for_template,
    plugin_ports,
    plugin_summary,
    plugin_template,
    required_parameter_names,
    secret_parameter_names,
    set_plugin_source,
    validate_plugin_manifest,
    with_plugin_secret_keys,
)


def _store_manifest(plugin_id: str) -> dict[str, Any] | None:
    from .store import get_installed_manifest

    return get_installed_manifest(plugin_id)


def _store_manifests() -> list[dict[str, Any]]:
    from . import store

    return [row["manifest"] for row in store.list_installed_plugins()]


set_plugin_source(CallablePluginSource(_store_manifest, _store_manifests))
