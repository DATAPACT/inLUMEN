"""The design agent's tools bound to the backend's Neo4j graph."""
from typing import Any

from inlumen.design.tools import build_pipeline_editor_tools as build_tools_for_store

from pipeline_agent.neo4j_store import Neo4jGraphStore


def build_pipeline_editor_tools(
    authorization: str | None = None,
    provenance_context: dict | None = None,
    workspace_id: str | None = None,
    preview_mode: bool = False,
) -> list[Any]:
    """Build request-scoped tools that mutate the persisted pipeline graph."""
    return build_tools_for_store(Neo4jGraphStore(
        authorization=authorization,
        provenance_context=provenance_context,
        workspace_id=workspace_id,
        preview_mode=preview_mode,
    ))
