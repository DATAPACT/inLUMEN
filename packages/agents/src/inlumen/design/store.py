"""Where the design agent's pipeline graph lives.

The agent's tools validate their arguments, resolve ports and templates, and
then call one :class:`GraphStore` operation per graph change. Each operation is
atomic and returns its result rows as JSON text: a list with one object per
affected record, each wrapping that record under one key, for example
``[{"step": {"flow_id": "3", ...}}]``. The tools show that text to the model,
so every store must return the same rows. An empty list means the operation's
preconditions did not hold and the graph was not changed.

Two stores implement this protocol:

- ``InMemoryGraphStore`` (``inlumen.design.memory_store``) keeps the graph in
  Python, for the benchmark and tests.
- The inLUMEN backend's ``Neo4jGraphStore`` runs one Cypher query per operation.

``tests/store_contract`` in this package checks that they behave the same.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

# Rows as JSON text, see the module docstring.
Rows = str


@dataclass(frozen=True)
class SubpipelineBinding:
    """The saved reusable pipeline a new Subpipeline step is pinned to."""

    reference: dict[str, str]
    input_ids: list[str]
    output_ids: list[str]


@dataclass(frozen=True)
class NewStep:
    """A STEP to create: its type and stored properties, in order.

    ``props`` holds raw values; stores escape them as they need.
    """

    step_type: str
    props: dict[str, Any]
    subpipeline: SubpipelineBinding | None = None


@dataclass(frozen=True)
class Predecessor:
    """An explicit predecessor for a new step (``create_step``'s after_flow_id)."""

    pipeline_uid: str
    flow_id: str
    source_port: str
    allow_fan_out: bool = False


@dataclass(frozen=True)
class FlowConfiguration:
    """A Flow step's behavior and the port handles its connections migrate to."""

    flow_id: str
    behavior: str
    parameters: dict[str, Any]
    ports_json: str
    input_port: str
    default_output_port: str


@dataclass(frozen=True)
class SubpipelineConfiguration:
    """A Subpipeline step's new reference and its connection migration."""

    flow_id: str
    pipeline_uid: str
    version_uid: str
    definition_json: str
    ports_json: str
    input_ids: list[str]
    output_ids: list[str]
    input_mapping: dict[str, str] = field(default_factory=dict)
    output_mapping: dict[str, str] = field(default_factory=dict)


class GraphStore(Protocol):
    """One design workspace: its design pipeline and the reusable catalog."""

    # Reads
    async def overview(self) -> Rows:
        """Pipeline, step and connection rows of the design pipeline."""

    async def get_graph(self) -> dict:
        """The design pipeline as a canvas graph (nodes, edges, pipeline)."""

    async def find_predecessor(self, flow_id: str) -> Rows:
        """``[{"predecessor": {pipeline_uid, flow_id, type, template_label,
        ports_json, primary_output_port}}]``."""

    async def connection_context(self, source_flow_id: str, target_flow_id: str) -> Rows:
        """Port context of a valid source → target pair, as ``connection_context``."""

    async def list_reusable_pipelines(self) -> Rows: ...

    async def find_reusable_pipeline_by_name(self, name: str) -> Rows: ...

    async def find_reusable_pipelines(
        self,
        *,
        pipeline_uid: str,
        pipeline_name: str,
        version_uid: str,
        version_name: str,
    ) -> Rows:
        """Up to two saved pipelines matching a uid, or a name (exact or contained)."""

    async def get_reusable_pipeline(self, pipeline_uid: str, version_uid: str) -> Rows: ...

    async def inspect_subpipeline_step(self, flow_id: str) -> Rows:
        """Current ports and connected handles of a Subpipeline step."""

    # Writes
    async def upsert_pipeline(self, name: str, description: str, version: str) -> Rows: ...

    async def create_step(
        self,
        step: NewStep,
        *,
        target_port: str,
        predecessor: Predecessor | None = None,
    ) -> Rows:
        """Create a step.

        With a predecessor, connect it after that step. Otherwise a Source starts
        its own branch, and any other step follows the graph's single tail.
        """

    async def connect_steps(
        self,
        source_flow_id: str,
        target_flow_id: str,
        source_port: str,
        target_port: str,
        allow_fan_out: bool,
    ) -> Rows: ...

    async def disconnect_steps(
        self,
        source_flow_id: str,
        target_flow_id: str,
        source_port: str,
        target_port: str,
    ) -> Rows: ...

    async def configure_flow_step(self, configuration: FlowConfiguration) -> Rows: ...

    async def create_reusable_pipeline(
        self,
        name: str,
        description: str,
        graph: dict,
        interface: dict,
        public_ports: dict,
    ) -> Rows: ...

    async def configure_subpipeline_step(self, configuration: SubpipelineConfiguration) -> Rows: ...

    async def insert_step(
        self,
        step: NewStep,
        *,
        before_flow_id: str,
        after_flow_id: str | None,
        new_input_port: str,
        new_output_port: str,
    ) -> Rows:
        """Insert before a step: between two connected steps, or as a new root."""

    async def delete_step(self, step_uid: str) -> Rows: ...

    async def delete_all_steps(self) -> Rows: ...

    async def restore_graph(self, graph: dict) -> None:
        """Replace the design pipeline with ``graph`` (rollback of a turn)."""


def decoded_rows(result: object) -> list[dict[str, Any]]:
    """The row objects of a store result; [] when it holds none."""
    decoded = result
    if isinstance(result, str):
        try:
            decoded = json.loads(result)
        except (TypeError, ValueError):
            return []
    if not isinstance(decoded, list):
        return []
    return [row for row in decoded if isinstance(row, dict)]


def returned_no_rows(result: object) -> bool:
    """True when a store result is an empty row list."""
    if isinstance(result, (list, tuple)):
        return len(result) == 0
    if isinstance(result, str):
        try:
            decoded = json.loads(result)
        except (TypeError, ValueError):
            return False
        return isinstance(decoded, list) and len(decoded) == 0
    return False
