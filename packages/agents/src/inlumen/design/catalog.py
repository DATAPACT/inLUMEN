"""What the design agent can build with: components, plugins, reusable pipelines."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from inlumen.contracts.node_definitions import NodeDefinitionRegistry
from inlumen.contracts.plugins import StaticPluginSource


def package_version() -> str:
    try:
        return version("inlumen-agents")
    except PackageNotFoundError:  # running from a source checkout
        return "0+unknown"


@dataclass(frozen=True)
class Catalog:
    """A snapshot of the components, installed plugins and reusable pipelines.

    ``plugins`` are plugin manifests; ``reusable_pipelines`` are items with a
    ``name``, a ``graph`` and optionally a ``description`` and ``uid``.
    """

    node_definitions: NodeDefinitionRegistry = field(default_factory=NodeDefinitionRegistry)
    plugins: tuple[dict[str, Any], ...] = ()
    reusable_pipelines: tuple[dict[str, Any], ...] = ()

    @classmethod
    def default(cls) -> "Catalog":
        """The core components, with no plugins and no reusable pipelines."""
        return cls()

    def plugin_source(self) -> StaticPluginSource:
        return StaticPluginSource(self.plugins)

    def fingerprint(self) -> str:
        """A content hash of everything the agent can use, and the package version."""
        content = {
            "package": package_version(),
            "components": [
                {"id": d.id, "version": d.version, "base_type": d.base_type,
                 "label": d.palette.label, "description": d.palette.description}
                for d in self.node_definitions.list()
            ],
            "plugins": sorted(self.plugins, key=lambda manifest: str(manifest.get("id"))),
            "reusable_pipelines": sorted(self.reusable_pipelines, key=lambda item: str(item.get("name"))),
        }
        encoded = json.dumps(content, sort_keys=True, ensure_ascii=True, default=str).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()
