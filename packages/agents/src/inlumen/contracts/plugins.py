"""Installed plugins, looked up through a pluggable plugin source.

A plugin is a manifest that bridges inLUMEN to a tool already deployed behind a
reachable REST API. Plugin-backed STEPs store ``template_label = "plugin:<id>"``.

Which plugins are installed depends on who uses this package: the inLUMEN
backend registers its plugin store with :func:`set_plugin_source`, and
``inlumen.design`` scopes the catalog of one design run with
:func:`use_plugin_source`. Without either, no plugins are installed.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from .plugin_manifest import (
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
    "PluginSource",
    "StaticPluginSource",
    "CallablePluginSource",
    "installed_plugin_manifest",
    "installed_plugin_manifests",
    "is_plugin_template",
    "plugin_id_from_template",
    "plugin_manifest_for_template",
    "plugin_ports",
    "plugin_summary",
    "plugin_template",
    "required_parameter_names",
    "secret_parameter_names",
    "set_plugin_source",
    "use_plugin_source",
    "validate_plugin_manifest",
    "with_plugin_secret_keys",
]


class PluginNotInstalled(ValueError):
    pass


class PluginSource(Protocol):
    """Where installed plugin manifests come from."""

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        """The pinned manifest of one installed plugin, or None."""

    def manifests(self) -> list[dict[str, Any]]:
        """Every installed plugin's manifest."""


class StaticPluginSource:
    """A fixed set of manifests, validated once (for snapshots and tests)."""

    def __init__(self, manifests: Iterable[Mapping[str, Any]] = ()):
        validated = [validate_plugin_manifest(dict(manifest)) for manifest in manifests]
        self._by_id = {manifest["id"]: manifest for manifest in validated}

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        return self._by_id.get(plugin_id)

    def manifests(self) -> list[dict[str, Any]]:
        return [self._by_id[key] for key in sorted(self._by_id)]


class CallablePluginSource:
    """Adapts two lookup functions, e.g. a database-backed plugin store."""

    def __init__(
        self,
        get: Callable[[str], dict[str, Any] | None],
        manifests: Callable[[], list[dict[str, Any]]],
    ):
        self._get = get
        self._manifests = manifests

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        return self._get(plugin_id)

    def manifests(self) -> list[dict[str, Any]]:
        return self._manifests()


_default_source: PluginSource = StaticPluginSource()
_scoped_source: ContextVar[PluginSource | None] = ContextVar("inlumen_plugin_source", default=None)


def set_plugin_source(source: PluginSource) -> None:
    """Set the process-wide plugin source (the inLUMEN backend's plugin store)."""
    global _default_source
    _default_source = source


@contextmanager
def use_plugin_source(source: PluginSource) -> Iterator[PluginSource]:
    """Use ``source`` for lookups in the current context (one design run)."""
    token = _scoped_source.set(source)
    try:
        yield source
    finally:
        _scoped_source.reset(token)


def active_plugin_source() -> PluginSource:
    return _scoped_source.get() or _default_source


def installed_plugin_manifest(plugin_id: str) -> dict[str, Any] | None:
    """Return the pinned manifest of an installed plugin."""
    return active_plugin_source().get(plugin_id)


def installed_plugin_manifests() -> list[dict[str, Any]]:
    """Return every installed plugin's manifest."""
    return active_plugin_source().manifests()


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
    from .node_parameters import is_sensitive_parameter_name, normalize_secret_param_keys

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
