"""The design agent: one serial, tool-using autogen agent over a graph store."""
from __future__ import annotations

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.models import ChatCompletionClient

from inlumen.contracts.node_definitions import NodeDefinitionRegistry

from .prompt import PIPELINE_EDITOR_DESCRIPTION, PIPELINE_EDITOR_SYSTEM_MESSAGE
from .store import GraphStore
from .tools import build_pipeline_editor_tools

PREVIEW_MODE_MESSAGE = (
    "\n\nPREVIEW MODE: Graph edits are isolated until the user accepts the proposal. "
    "You may inspect and use existing reusable pipelines, but creating a new reusable "
    "catalog item cannot be previewed. If the user requests one, explain that they can "
    "turn off Preview graph changes and send that request again."
)


def build_design_team(
    model_client: ChatCompletionClient,
    store: GraphStore,
    *,
    preview_mode: bool = False,
    node_definitions: NodeDefinitionRegistry | None = None,
    max_tool_iterations: int = 30,
) -> RoundRobinGroupChat:
    """Assemble one serial, tool-using agent for a request-scoped graph session.

    Pipeline writes share ordering and flow-id state, so the model client must
    not make parallel tool calls.
    """
    tools = build_pipeline_editor_tools(store, node_definitions=node_definitions)
    system_message = PIPELINE_EDITOR_SYSTEM_MESSAGE
    if preview_mode:
        system_message += PREVIEW_MODE_MESSAGE
    editor = AssistantAgent(
        name="pipeline_editor",
        model_client=model_client,
        tools=tools,
        description=PIPELINE_EDITOR_DESCRIPTION,
        system_message=system_message,
        max_tool_iterations=max_tool_iterations,
        reflect_on_tool_use=True,
    )
    return RoundRobinGroupChat([editor], max_turns=1)
