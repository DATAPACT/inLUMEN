"""High-level construction of the pipeline editing agent."""

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat

from llm_config import LLMConfig, log_llm_selection, select_model_client

from pipeline_agent.prompt import (
    PIPELINE_EDITOR_DESCRIPTION,
    PIPELINE_EDITOR_SYSTEM_MESSAGE,
)
from pipeline_agent.tools import build_pipeline_editor_tools


def build_pipeline_editing_team(
    llm_config: LLMConfig,
    authorization: str | None = None,
    provenance_context: dict | None = None,
    workspace_id: str | None = None,
    preview_mode: bool = False,
) -> RoundRobinGroupChat:
    """Assemble one serial, tool-using agent for a request-scoped graph session."""
    log_llm_selection("Building pipeline editing team", llm_config)

    # Pipeline writes share ordering and flow-id state. Parallel tool calls can
    # deadlock Neo4j or connect concurrently created components arbitrarily.
    model_client = select_model_client(llm_config, parallel_tool_calls=False)
    tools = build_pipeline_editor_tools(
        authorization=authorization,
        provenance_context=provenance_context,
        workspace_id=workspace_id,
        preview_mode=preview_mode,
    )
    system_message = PIPELINE_EDITOR_SYSTEM_MESSAGE
    if preview_mode:
        system_message += (
            "\n\nPREVIEW MODE: Graph edits are isolated until the user accepts the proposal. "
            "You may inspect and use existing reusable pipelines, but creating a new reusable "
            "catalog item cannot be previewed. If the user requests one, explain that they can "
            "turn off Preview graph changes and send that request again."
        )
    editor = AssistantAgent(
        name="pipeline_editor",
        model_client=model_client,
        tools=tools,
        description=PIPELINE_EDITOR_DESCRIPTION,
        system_message=system_message,
        max_tool_iterations=30,
        reflect_on_tool_use=True,
    )
    return RoundRobinGroupChat([editor], max_turns=1)
