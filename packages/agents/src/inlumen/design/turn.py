"""One design turn: run the agent, check the result, repair once.

The caller owns everything around the turn: building the team, reconciling the
visible canvas, saving the result, chat history and cancellation. This module
only runs the agent against a graph and judges what it produced.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from .context import _assistant_message_from_result, _build_agent_task, is_read_only_pipeline_request
from .guardrails import _build_graph_sync_guardrail, _guardrail_repair_task


class GraphAccess(Protocol):
    """What a turn needs from the graph: read it, and put a snapshot back.

    Every :class:`~inlumen.design.store.GraphStore` provides both.
    """

    async def get_graph(self) -> dict: ...

    async def restore_graph(self, graph: dict) -> None: ...


@dataclass
class DesignTurn:
    """The outcome of one turn, before the caller saves or rolls it back.

    ``sync`` is the guardrail verdict: ``guardrail_passed``, ``status``
    (``"invalid"`` when the graph failed validation), ``graph_changed``,
    ``validation_errors`` and a ``message``.
    """

    assistant_message: str
    graph: dict | None
    sync: dict[str, Any]
    repaired: bool = False


async def _read_graph(graph: GraphAccess) -> tuple[dict | None, str | None]:
    try:
        return await graph.get_graph(), None
    except Exception as exc:
        return None, str(exc)


async def run_design_turn(
    *,
    user_message: str,
    team: Any,
    graph: GraphAccess,
    canvas_graph: dict | None,
    agent_graph: dict | None,
    visible_before_graph: dict | None,
    restore_read_only_changes: bool = True,
) -> DesignTurn:
    """Run the agent on ``user_message`` and validate, and if needed repair, the result.

    ``canvas_graph`` is what the user sees, ``agent_graph`` the stored graph the
    agent edits, and ``visible_before_graph`` the snapshot that changes are
    judged against. An informational request must not change the graph: if it
    did, the snapshot is put back (through ``graph.restore_graph`` when
    ``restore_read_only_changes``; otherwise the change is only discarded from
    the result, e.g. in a throwaway preview workspace).
    """
    result = await team.run(task=_build_agent_task(user_message, canvas_graph, agent_graph))
    assistant_message = _assistant_message_from_result(result)
    after_graph, after_error = await _read_graph(graph)
    sync = _build_graph_sync_guardrail(visible_before_graph, after_graph, user_message, after_error)

    # Informational requests must never turn model paraphrasing or an accidental
    # tool write into a real edit or a Review AI proposal.
    if is_read_only_pipeline_request(user_message) and sync.get("graph_changed"):
        if restore_read_only_changes:
            if not isinstance(visible_before_graph, dict):
                raise RuntimeError("No pre-turn graph snapshot is available for a read-only request.")
            try:
                await graph.restore_graph(visible_before_graph)
                after_graph = deepcopy(visible_before_graph)
                after_error = None
            except Exception as exc:
                after_error = str(exc)
        else:
            after_graph = deepcopy(visible_before_graph)
            after_error = None
        sync = _build_graph_sync_guardrail(visible_before_graph, after_graph, user_message, after_error)

    repaired = False
    if not sync["guardrail_passed"] and sync.get("status") == "invalid" and not after_error:
        repair_result = await team.run(
            task=_guardrail_repair_task(
                user_message,
                canvas_graph,
                after_graph,
                sync.get("validation_errors"),
            )
        )
        repaired = True
        repair_message = _assistant_message_from_result(repair_result)
        if repair_message:
            assistant_message = repair_message
        after_graph, repaired_error = await _read_graph(graph)
        sync = _build_graph_sync_guardrail(
            visible_before_graph,
            after_graph,
            user_message,
            repaired_error,
            repaired=True,
        )

    return DesignTurn(assistant_message, after_graph, sync, repaired)
