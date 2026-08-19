"""Shared contract helpers for the pipeline-editing agent.

The editor agent is allowed to compose the same five structural components that
the palette exposes.  Keeping the strict agent boundary here avoids scattering
type checks and port fallbacks through each graph-mutation tool.
"""

from __future__ import annotations

from typing import Any

from step_types import CANONICAL_STEP_TYPES


COMPONENT_DEFINITION_IDS = {
    "source": "core.source",
    "task": "core.task",
    "destination": "core.destination",
    "flow": "core.flow",
    "subpipeline": "core.subpipeline",
}

SEMANTIC_IMPLEMENTATION_KIND_ALIASES = {
    "trusted-pretrained-inference": ("generated-code", "trusted_heavy_model"),
    "trusted_pretrained_inference": ("generated-code", "trusted_heavy_model"),
    "classical-ml-training": ("generated-code", "classical_ml"),
    "classical_ml_training": ("generated-code", "classical_ml"),
    "deterministic-processing": ("generated-code", "deterministic"),
    "deterministic_processing": ("generated-code", "deterministic"),
}

# Connector selection is intentionally separate from the artifact contract:
# these values only describe what a boundary needs before it can run.  Custom
# is the safe default and has no platform-owned parameters.
CONNECTOR_REQUIRED_PARAMETERS = {
    "source": {
        "database": ("connection_url", "query"),
        "object storage": ("bucket",),
        "rest api": ("url",),
        "stream/kafka": ("brokers", "topic"),
        "kafka": ("brokers", "topic"),
        "message queue": ("url", "queue"),
    },
    "destination": {
        "file": ("filename",),
        "database": ("connection_url", "table"),
        "object storage": ("bucket",),
        "rest api": ("url",),
        "stream/kafka": ("brokers", "topic"),
        "kafka": ("brokers", "topic"),
        "message queue": ("url", "queue"),
        "notification": ("channel",),
    },
}


def missing_connector_parameters(
    step_type: object,
    template: object,
    parameters: Any,
) -> list[str]:
    """Return required boundary settings absent from a connector node."""
    kind = str(step_type or "").strip().lower()
    connector = str(template or "").strip().lower()
    required = CONNECTOR_REQUIRED_PARAMETERS.get(kind, {}).get(connector, ())
    values = parameters if isinstance(parameters, dict) else {}
    return [
        name for name in required
        if values.get(name) is None or not str(values.get(name)).strip()
    ]


def require_agent_step_type(raw_type: object) -> str:
    """Return an exact palette type, rejecting aliases and invented boxes."""
    normalized = str(raw_type or "").strip().lower()
    if normalized not in CANONICAL_STEP_TYPES:
        available = ", ".join(sorted(CANONICAL_STEP_TYPES))
        raise ValueError(
            f"type must be one of the available Pipeline Components: {available}"
        )
    return normalized


def validate_insertion_kind(step_type: str, *, initial: bool) -> None:
    """Reject placements that necessarily violate the directed graph contract."""
    if initial and step_type != "source":
        raise ValueError("An initial insertion must use the Source component")
    if not initial and step_type in {"source", "destination"}:
        raise ValueError(
            "Only Task, Flow, or Subpipeline components can be inserted between steps"
        )


def normalize_agent_implementation(implementation: Any) -> dict[str, Any]:
    """Keep runtime packaging separate from a task's semantic execution class."""
    candidate = dict(implementation) if isinstance(implementation, dict) else {}
    raw_kind = str(candidate.get("kind") or "").strip().lower()
    alias = SEMANTIC_IMPLEMENTATION_KIND_ALIASES.get(raw_kind)
    if alias:
        candidate["kind"] = alias[0]
        candidate.setdefault("execution_profile", alias[1])
    kind = str(candidate.get("kind") or "").strip().lower()
    if kind and kind not in {"python", "generated-code"}:
        raise ValueError(
            "Task implementation.kind must be 'generated-code' or 'python'. "
            "Sources and Destinations are configured as connectors during bundle generation."
        )
    return candidate


def default_input_port_expression(variable: str) -> str:
    """Cypher expression for legacy nodes that lack primary_input_port."""
    return (
        f"coalesce({variable}.primary_input_port, CASE "
        f"WHEN {variable}.type = 'flow' AND toLower(coalesce({variable}.template_label, '')) = 'condition' THEN 'value' "
        f"WHEN {variable}.type = 'flow' AND toLower(coalesce({variable}.template_label, '')) = 'parallel map' THEN 'items' "
        f"WHEN {variable}.type = 'destination' THEN 'data' "
        "ELSE 'input' END)"
    )


def default_output_port_expression(variable: str) -> str:
    """Cypher expression for legacy nodes that lack primary_output_port."""
    return (
        f"coalesce({variable}.primary_output_port, CASE "
        f"WHEN {variable}.type = 'source' THEN 'data' "
        f"WHEN {variable}.type = 'flow' AND toLower(coalesce({variable}.template_label, '')) = 'condition' THEN 'when_true' "
        f"WHEN {variable}.type = 'flow' AND toLower(coalesce({variable}.template_label, '')) = 'parallel map' THEN 'item' "
        "ELSE 'output' END)"
    )
