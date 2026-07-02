from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Iterable

from .schema import NodeDefinition, NodeDefinitionValidationError


DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


class NodeDefinitionRegistry:
    """Loads and validates versioned node definitions from JSON manifests."""

    def __init__(self, manifest_dir: Path | str = DEFAULT_MANIFEST_DIR):
        self.manifest_dir = Path(manifest_dir)
        self._definitions = self._load()

    def _load(self) -> dict[str, NodeDefinition]:
        if not self.manifest_dir.is_dir():
            raise NodeDefinitionValidationError(
                f"node definition manifest directory not found: {self.manifest_dir}"
            )

        definitions: dict[str, NodeDefinition] = {}
        manifest_paths = sorted(self.manifest_dir.glob("*.json"))
        if not manifest_paths:
            raise NodeDefinitionValidationError(
                f"no node definition manifests found in {self.manifest_dir}"
            )

        for manifest_path in manifest_paths:
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NodeDefinitionValidationError(
                    f"invalid JSON in {manifest_path.name}: {exc}"
                ) from exc

            raw_definitions = (
                payload.get("definitions")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(raw_definitions, list):
                raise NodeDefinitionValidationError(
                    f"{manifest_path.name} must contain a definitions array"
                )

            for raw_definition in raw_definitions:
                try:
                    definition = NodeDefinition.from_dict(raw_definition)
                except NodeDefinitionValidationError as exc:
                    raise NodeDefinitionValidationError(
                        f"{manifest_path.name}: {exc}"
                    ) from exc
                if definition.id in definitions:
                    raise NodeDefinitionValidationError(
                        f"duplicate node definition id: {definition.id}"
                    )
                definitions[definition.id] = definition

        return definitions

    def get(self, definition_id: str) -> NodeDefinition | None:
        return self._definitions.get(str(definition_id or "").strip())

    def list(self, *, include_disabled: bool = False) -> list[NodeDefinition]:
        definitions: Iterable[NodeDefinition] = self._definitions.values()
        if not include_disabled:
            definitions = (definition for definition in definitions if definition.enabled)
        return sorted(
            definitions,
            key=lambda definition: (
                definition.palette.order,
                definition.family,
                definition.palette.label.lower(),
            ),
        )

    def response_payload(self, *, include_disabled: bool = False) -> dict:
        return {
            "schema_version": 1,
            "definitions": [
                definition.to_dict()
                for definition in self.list(include_disabled=include_disabled)
            ],
        }


_registry: NodeDefinitionRegistry | None = None
_registry_lock = Lock()


def get_node_definition_registry() -> NodeDefinitionRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = NodeDefinitionRegistry()
    return _registry
