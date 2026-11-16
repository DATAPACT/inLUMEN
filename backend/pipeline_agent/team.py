"""High-level construction of the pipeline editing agent."""

from inlumen.design.team import build_design_team
from llm_config import LLMConfig, log_llm_selection, select_model_client

from pipeline_agent.neo4j_store import Neo4jGraphStore


def build_pipeline_editing_team(
    llm_config: LLMConfig,
    authorization: str | None = None,
    provenance_context: dict | None = None,
    workspace_id: str | None = None,
    preview_mode: bool = False,
    store: Neo4jGraphStore | None = None,
):
    """Assemble one serial, tool-using agent for a request-scoped graph session."""
    log_llm_selection("Building pipeline editing team", llm_config)

    # Pipeline writes share ordering and flow-id state. Parallel tool calls can
    # deadlock Neo4j or connect concurrently created components arbitrarily.
    model_client = select_model_client(llm_config, parallel_tool_calls=False)
    store = store or Neo4jGraphStore(
        authorization=authorization,
        provenance_context=provenance_context,
        workspace_id=workspace_id,
        preview_mode=preview_mode,
    )
    return build_design_team(model_client, store, preview_mode=preview_mode)
