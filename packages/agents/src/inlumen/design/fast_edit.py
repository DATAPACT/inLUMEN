"""One small model call for a plain delete, clear or connect request.

The full agent sends its rules, every tool description and the canvas with
each model call, and a simple edit takes two to four calls. Here the model
only names the action and the steps; code runs the tool. Anything that is
not exactly one of the three actions, and any doubt or failure, goes to the
full agent unchanged.
"""
from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from autogen_core import CancellationToken
from autogen_core.models import AssistantMessage, ChatCompletionClient, SystemMessage, UserMessage

from .context import _graph_for_agent_context, is_read_only_pipeline_request

# Longer requests are rarely one plain action; they go to the full agent.
MAX_REQUEST_CHARS = 300
# The compact canvas is sent whole. A large canvas makes the call less small
# and the choice of step less certain, so it goes to the full agent too.
MAX_CANVAS_STEPS = 40

ROUTER_SYSTEM_MESSAGE = """You route one request about an existing data pipeline.
Reply with exactly one JSON object and nothing else.

Possible replies:
{"action": "delete_step", "flow_id": "<id of the step>"}
{"action": "delete_all_steps"}
{"action": "connect_steps", "source_flow_id": "<id>", "target_flow_id": "<id>"}
{"action": "other"}

Rules:
- Choose delete_step only when the request asks to remove exactly one step and
  that step is identified without doubt among the canvas nodes.
- Choose delete_all_steps only when the request asks to clear the whole canvas
  or to remove every step.
- Choose connect_steps only when the request asks to add exactly one connection
  between two existing steps that are both identified without doubt. The
  source is the step whose output is used.
- Choose other for everything else: questions, new steps, renames, changed
  descriptions, replacements, moved or inserted steps, removed connections,
  several actions in one request, references to earlier messages ("undo that",
  "the same for the other one"), and whenever you are not sure.
- Use the node ids of the canvas exactly as given. Never invent an id."""

_ACTIONS = {"delete_step", "delete_all_steps", "connect_steps"}


@dataclass
class FastEditOutcome:
    """A simple edit that was carried out without the full agent."""

    action: str
    message: str


def fast_edits_enabled() -> bool:
    """Off with INLUMEN_DESIGN_FAST_EDITS=0 (or false/off/no)."""
    return os.environ.get("INLUMEN_DESIGN_FAST_EDITS", "1").strip().lower() not in {"0", "false", "off", "no"}


def _first_json_object(text: str) -> dict | None:
    """The reply's JSON object, tolerating code fences or a sentence around it."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _rows(text: str) -> list[dict] | None:
    """Decode a tool result (the repr of JSON text holding a list of rows)."""
    try:
        value: Any = ast.literal_eval(text)
        if isinstance(value, str):
            value = json.loads(value)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
        return value
    return None


class FastEdit:
    """Tries a request as one plain edit; the caller validates or undoes it."""

    def __init__(
        self,
        model_client: ChatCompletionClient,
        store: Any,
        tools: Mapping[str, Any],
        *,
        remember: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._model_client = model_client
        self._store = store
        self._tools = tools
        self._remember = remember
        self._snapshot: dict | None = None

    def applies_to(self, user_message: str, canvas: dict | None) -> bool:
        nodes = canvas.get("nodes") if isinstance(canvas, dict) else None
        text = str(user_message or "").strip()
        return (
            fast_edits_enabled()
            and isinstance(nodes, list)
            and 0 < len(nodes) <= MAX_CANVAS_STEPS
            and 0 < len(text) <= MAX_REQUEST_CHARS
            and not is_read_only_pipeline_request(text)
        )

    async def _route(self, user_message: str, canvas: dict) -> tuple[dict | None, dict[str, str]]:
        compact = _graph_for_agent_context(canvas)
        labels = {str(node.get("id")): str(node.get("label") or node.get("id")) for node in compact["nodes"]}
        for node in compact["nodes"]:
            # Positions say nothing about which step is meant.
            node.pop("x", None)
            node.pop("y", None)
        result = await self._model_client.create([
            SystemMessage(content=ROUTER_SYSTEM_MESSAGE),
            UserMessage(
                content=(
                    f"REQUEST:\n{user_message}\n\nCANVAS:\n"
                    + json.dumps({"nodes": compact["nodes"], "edges": compact["edges"]}, ensure_ascii=False)
                ),
                source="user",
            ),
        ])
        content = result.content if isinstance(result.content, str) else ""
        return _first_json_object(content), labels

    async def try_edit(self, user_message: str, canvas: dict | None) -> FastEditOutcome | None:
        """Carry out the request if it is one plain edit; None sends it to the agent.

        After a returned outcome the graph has changed. The caller checks it and
        calls :meth:`undo` when the result is not acceptable.
        """
        self._snapshot = None
        if not self.applies_to(user_message, canvas):
            return None
        try:
            choice, labels = await self._route(user_message, canvas)  # type: ignore[arg-type]
        except Exception:
            return None
        action = str((choice or {}).get("action") or "")
        if action not in _ACTIONS:
            return None

        def known(key: str) -> str | None:
            value = str(choice.get(key) or "").strip()  # type: ignore[union-attr]
            return value if value in labels else None

        if action == "delete_step":
            flow_id = known("flow_id")
            if not flow_id:
                return None
            arguments: dict[str, Any] = {"flow_id": flow_id}
        elif action == "connect_steps":
            source, target = known("source_flow_id"), known("target_flow_id")
            if not source or not target or source == target:
                return None
            # The user named both ends, so a second connection from the source
            # is the request itself and not an accident.
            arguments = {"source_flow_id": source, "target_flow_id": target, "allow_fan_out": True}
        else:
            arguments = {}

        try:
            self._snapshot = await self._store.get_graph()
            text = await self._tools[action].run_json(
                {"params": json.dumps(arguments)}, CancellationToken()
            )
        except Exception:
            await self.undo()
            return None
        rows = _rows(text) if isinstance(text, str) else None
        if rows is None:
            # No rows, or a note that the edit is not valid alone: the full
            # agent explains or completes such a request.
            await self.undo()
            return None
        return FastEditOutcome(action, self._message(action, rows[0], labels))

    @staticmethod
    def _message(action: str, row: dict, labels: dict[str, str]) -> str:
        if action == "delete_step":
            deleted = row.get("deleted_step") or {}
            label = deleted.get("label") or labels.get(str(deleted.get("flow_id")), "the step")
            if deleted.get("chain_reconnected"):
                return f"I removed “{label}” and connected the step before it to the step after it."
            return f"I removed “{label}”."
        if action == "connect_steps":
            connection = row.get("connection") or {}
            source = connection.get("source_label") or labels.get(str(connection.get("source_flow_id")), "the source")
            target = connection.get("target_label") or labels.get(str(connection.get("target_flow_id")), "the target")
            return f"I connected “{source}” to “{target}”."
        count = (row.get("pipeline") or {}).get("deleted_step_count")
        return f"I removed all {count} steps. The canvas is empty." if count else "I cleared the canvas."

    async def undo(self) -> bool:
        """Put the graph back as it was before the edit. True when it is back."""
        snapshot, self._snapshot = self._snapshot, None
        if snapshot is None:
            return True
        try:
            await self._store.restore_graph(snapshot)
        except Exception:
            return False
        note = getattr(self._store, "tool_call_rolled_back", None)
        if note is not None:
            try:
                await note()
            except Exception:
                pass
        return True

    async def remember(self, user_message: str, outcome: FastEditOutcome) -> None:
        """Add the exchange to the agent's chat memory, so a follow-up can refer to it."""
        if self._remember is not None:
            try:
                await self._remember(user_message, outcome.message)
            except Exception:
                pass  # the edit stands; only the memory of it is missing


def agent_memory_writer(agent: Any) -> Callable[[str, str], Awaitable[None]]:
    """Writes a request and its answer into an autogen agent's model context."""

    async def write(user_message: str, reply: str) -> None:
        context = agent.model_context
        await context.add_message(UserMessage(content=user_message, source="user"))
        await context.add_message(AssistantMessage(content=reply, source=agent.name))

    return write
