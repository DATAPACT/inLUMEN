"""The design agent: one serial, tool-using autogen agent over a graph store."""
from __future__ import annotations

import contextlib
import inspect
import json
from collections.abc import Mapping
from typing import Any

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.models import ChatCompletionClient
from autogen_core.tools import FunctionTool

from inlumen.contracts.node_definitions import NodeDefinitionRegistry

from .guardrails import EditWarnings
from .prompt import PIPELINE_EDITOR_DESCRIPTION, PIPELINE_EDITOR_SYSTEM_MESSAGE
from .store import GraphStore
from .tools import build_pipeline_editor_tools, pipeline_palette

PREVIEW_MODE_MESSAGE = (
    "\n\nPREVIEW MODE: Graph edits are isolated until the user accepts the proposal. "
    "You may inspect and use existing reusable pipelines, but creating a new reusable "
    "catalog item cannot be previewed. If the user requests one, explain that they can "
    "turn off Preview graph changes and send that request again."
)


# Fields the design tools refuse. Their values may be secrets a user pasted
# into the chat, so they are never written to the record of a tool call.
_REFUSED_FIELDS = {"parameters", "secret_parameters", "implementation", "param", "secret_params"}


def _without_refused_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[removed]" if key in _REFUSED_FIELDS else _without_refused_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_refused_values(item) for item in value]
    return value


def _arguments_for_record(arguments: Mapping[str, Any]) -> str:
    """A tool call's arguments as JSON text, decoded once and without refused values."""
    payload: Any = arguments.get("params", arguments) if isinstance(arguments, Mapping) else arguments
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return "[not valid JSON]"
    return json.dumps(_without_refused_values(payload), ensure_ascii=False, default=str)


class TolerantFunctionTool(FunctionTool):
    """A design tool that accepts its JSON however the model sends it.

    Every tool declares one string argument, ``params``, holding JSON. Some
    models send that JSON as an object instead, or send the object without the
    ``params`` wrapper. Both would be rejected before the tool runs and cost a
    full retry, so they are turned into the declared form here.
    """

    def __init__(self, func: Any, store: Any = None) -> None:
        super().__init__(func, description=inspect.getdoc(func) or "", name=func.__name__)
        self.__name__ = func.__name__
        self._store = store
        self._takes_params = "params" in inspect.signature(func).parameters

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self._func(*args, **kwargs)

    async def run_json(self, args: Mapping[str, Any], cancellation_token: Any, call_id: str | None = None) -> Any:
        """Run the tool and report the call to the store, whatever its outcome.

        A store that keeps provenance (``tool_call`` and ``tool_call_finished``)
        records every call the agent makes: reads, failed calls and repeated
        calls each count as one.
        """
        arguments = self.normalized(args)
        scope = getattr(self._store, "tool_call", None)
        finished = getattr(self._store, "tool_call_finished", None)
        with scope(self.__name__) if scope else contextlib.nullcontext():
            error = None
            try:
                return await super().run_json(arguments, cancellation_token, call_id)
            except BaseException as exc:
                error = str(exc) or type(exc).__name__
                raise
            finally:
                if finished is not None:
                    try:
                        await finished(self.__name__, _arguments_for_record(arguments), error)
                    except Exception:
                        pass  # the record is missing; the tool's own result stands

    def normalized(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._takes_params or not isinstance(args, Mapping):
            return args
        if "params" not in args:
            # The bare object: {"steps": [...]} instead of {"params": "{...}"}.
            return {"params": json.dumps(dict(args), ensure_ascii=False)}
        params = args["params"]
        if isinstance(params, str):
            return args
        return {"params": json.dumps({} if params is None else params, ensure_ascii=False)}


class ProviderAwareGroupChat(RoundRobinGroupChat):
    def __init__(self, *args, model_client=None, **kwargs):
        super().__init__(*args, **kwargs)
        from inlumen.llm_autogen import OpenAIResponsesClient
        self._responses_client = model_client if isinstance(model_client, OpenAIResponsesClient) else None

    async def save_state(self):
        state = dict(await super().save_state())
        if self._responses_client is not None:
            state["responses_replay"] = self._responses_client.save_replay()
        return state

    async def load_state(self, state):
        state = dict(state)
        replay = state.pop("responses_replay", {})
        await super().load_state(state)
        if self._responses_client is not None:
            self._responses_client.load_replay(replay)

    async def run_stream(self, **kwargs):
        from inlumen.llm_requests import provider_error_scope
        with provider_error_scope():
            async for item in super().run_stream(**kwargs):
                yield item


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
    edit_warnings = EditWarnings()
    tools = [
        TolerantFunctionTool(tool, store)
        for tool in build_pipeline_editor_tools(
            store, node_definitions=node_definitions, edit_warnings=edit_warnings
        )
    ]
    system_message = PIPELINE_EDITOR_SYSTEM_MESSAGE + (
        "\n\nPIPELINE COMPONENTS PALETTE (authoritative; what list_pipeline_components returns)\n"
        + json.dumps(pipeline_palette(node_definitions), ensure_ascii=False, sort_keys=True)
    )
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
    team = ProviderAwareGroupChat([editor], max_turns=1, model_client=model_client)
    # Read by run_design_turn to tell a declined edit from an unfinished one.
    team.edit_warnings = edit_warnings
    return team
