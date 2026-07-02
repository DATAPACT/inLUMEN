from __future__ import annotations

from typing import Any

from node_definitions import get_node_definition_registry

from .base import GeneratedRuntimeArtifacts, NodeGenerator


class GeneratorRegistry:
    """Resolve deterministic node generators from node-definition manifests."""

    def __init__(self, generators: dict[str, NodeGenerator] | None = None):
        self._generators: dict[str, NodeGenerator] = generators or {}

    def get(self, generator_name: str) -> NodeGenerator | None:
        return self._generators.get(str(generator_name or "").strip())

    def generator_for_step(self, step: dict[str, Any]) -> NodeGenerator | None:
        definition_id = str(step.get("definition_id") or "").strip()
        if not definition_id:
            return None
        definition = get_node_definition_registry().get(definition_id)
        if definition is None:
            return None
        return self.get(definition.runtime.generator)

    def generate(
        self,
        step: dict[str, Any],
        graph: dict[str, Any] | None = None,
    ) -> GeneratedRuntimeArtifacts:
        generator = self.generator_for_step(step)
        if generator is None:
            definition_id = str(step.get("definition_id") or "").strip()
            raise ValueError(
                f"No deterministic generator is registered for {definition_id!r}."
            )
        return generator.generate(step, graph)


_registry = GeneratorRegistry()


def generate_runtime_artifacts(
    step: dict[str, Any],
    graph: dict[str, Any] | None = None,
) -> GeneratedRuntimeArtifacts:
    return _registry.generate(step, graph)
