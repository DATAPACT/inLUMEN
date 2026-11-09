"""What a node declares about its own outputs, and what follows from it.

A node's declared outputs (its output artifacts and typed output ports) are the
contract the graph states. Inference from a node's wording is only a fallback
for nodes that declare nothing: once a node declares its outputs, they decide
the contract and whether the node trains a model.
"""

from __future__ import annotations

from typing import Any

from .port_types import parse_port_type

# Implementation keys that state the node's output contract. Model plans add
# runtime settings next to them and never replace them.
DECLARED_OUTPUT_KEYS = ("output_artifacts", "output_artifact")

MODEL_KIND = "model"


def declared_output_artifacts(implementation: Any) -> list[dict[str, Any]]:
    """The output artifacts an implementation payload declares (may be empty)."""
    source = implementation if isinstance(implementation, dict) else {}
    plural = source.get("output_artifacts")
    if isinstance(plural, list):
        return [item for item in plural if isinstance(item, dict)]
    single = source.get("output_artifact")
    return [single] if isinstance(single, dict) else []


def typed_output_ports(ports: Any) -> list[dict[str, Any]]:
    """Output ports with a declared type; untyped default ports declare nothing."""
    outputs = ports.get("outputs") if isinstance(ports, dict) else None
    if not isinstance(outputs, list):
        return []
    return [
        port for port in outputs
        if isinstance(port, dict) and parse_port_type(port.get("type")).kind != "any"
    ]


def declares_model_output(implementation: Any = None, ports: Any = None) -> bool | None:
    """Whether the node's declared outputs include a trained model.

    None when the node declares no outputs at all, so the caller may fall back
    to inference; otherwise True only if an artifact or output port is a model.
    """
    artifacts = declared_output_artifacts(implementation)
    typed_ports = typed_output_ports(ports)
    if not artifacts and not typed_ports:
        return None
    return any(_is_model_artifact(item) for item in artifacts) or any(
        parse_port_type(port.get("type")).kind == MODEL_KIND for port in typed_ports)


def _is_model_artifact(item: dict[str, Any]) -> bool:
    """A model artifact, or a bundle with a model among its members."""
    if str(item.get("kind") or "").strip().lower() == MODEL_KIND:
        return True
    members = item.get("members")
    return isinstance(members, list) and any(
        isinstance(member, dict) and _is_model_artifact(member) for member in members)


def with_declared_outputs(resolved: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """The resolved plan, keeping the output contract the candidate declared."""
    for key in DECLARED_OUTPUT_KEYS:
        if key in candidate and key not in resolved:
            resolved[key] = candidate[key]
    return resolved
