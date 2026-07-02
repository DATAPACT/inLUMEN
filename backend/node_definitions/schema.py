from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from step_types import CANONICAL_STEP_TYPES


class NodeDefinitionValidationError(ValueError):
    """Raised when a node-definition manifest does not satisfy the contract."""


def _required_string(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise NodeDefinitionValidationError(f"{field_name} must be a non-empty string")
    return normalized


def _optional_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NodeDefinitionValidationError(f"{field_name} must be an object")
    return dict(value)


@dataclass(frozen=True)
class NodePaletteMetadata:
    label: str
    description: str
    icon: str
    color: str
    order: int = 0

    @classmethod
    def from_dict(cls, value: Any) -> "NodePaletteMetadata":
        data = _optional_mapping(value, "palette")
        try:
            order = int(data.get("order", 0))
        except (TypeError, ValueError) as exc:
            raise NodeDefinitionValidationError("palette.order must be an integer") from exc
        return cls(
            label=_required_string(data.get("label"), "palette.label"),
            description=str(data.get("description") or "").strip(),
            icon=_required_string(data.get("icon"), "palette.icon"),
            color=_required_string(data.get("color"), "palette.color"),
            order=order,
        )


@dataclass(frozen=True)
class NodeEditorDescriptor:
    kind: str
    catalog: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "NodeEditorDescriptor":
        data = _optional_mapping(value, "editor")
        catalog = str(data.get("catalog") or "").strip() or None
        return cls(
            kind=_required_string(data.get("kind", "default"), "editor.kind"),
            catalog=catalog,
        )


@dataclass(frozen=True)
class NodeRuntimeDescriptor:
    generator: str
    template: str | None = None
    base_image: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "NodeRuntimeDescriptor":
        data = _optional_mapping(value, "runtime")
        template = str(data.get("template") or "").strip() or None
        base_image = str(data.get("base_image") or "").strip() or None
        return cls(
            generator=_required_string(data.get("generator", "generic"), "runtime.generator"),
            template=template,
            base_image=base_image,
        )


@dataclass(frozen=True)
class NodeDefinition:
    id: str
    version: int
    base_type: str
    family: str
    palette: NodePaletteMetadata
    editor: NodeEditorDescriptor
    runtime: NodeRuntimeDescriptor
    operation: str | None = None
    enabled: bool = True
    default_implementation: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Any) -> "NodeDefinition":
        data = _optional_mapping(value, "definition")
        definition_id = _required_string(data.get("id"), "id")
        if "." not in definition_id:
            raise NodeDefinitionValidationError(
                f"id must be namespaced with a dot: {definition_id!r}"
            )
        try:
            version = int(data.get("version"))
        except (TypeError, ValueError) as exc:
            raise NodeDefinitionValidationError("version must be a positive integer") from exc
        if version < 1:
            raise NodeDefinitionValidationError("version must be a positive integer")

        base_type = _required_string(data.get("base_type"), "base_type").lower()
        if base_type not in CANONICAL_STEP_TYPES:
            raise NodeDefinitionValidationError(
                f"base_type must be one of {sorted(CANONICAL_STEP_TYPES)}"
            )

        operation = str(data.get("operation") or "").strip() or None
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise NodeDefinitionValidationError("enabled must be a boolean")

        return cls(
            id=definition_id,
            version=version,
            base_type=base_type,
            family=_required_string(data.get("family"), "family"),
            operation=operation,
            enabled=enabled,
            palette=NodePaletteMetadata.from_dict(data.get("palette")),
            editor=NodeEditorDescriptor.from_dict(data.get("editor")),
            runtime=NodeRuntimeDescriptor.from_dict(data.get("runtime")),
            default_implementation=_optional_mapping(
                data.get("default_implementation"),
                "default_implementation",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
