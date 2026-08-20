"""Request-independent lifecycle for one pipeline editor turn."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass

from chat_state import clear_state_from_disk, load_state_from_disk, save_state_to_disk
from graph_client import (
    fetch_pipeline_graph,
    save_active_pipeline_version,
    sync_backend_to_canvas_graph,
)
from llm_config import LLMConfig
from pipeline_agent.context import (
    _assistant_message_from_result,
    _build_agent_task,
    _graph_counts,
    _safe_assistant_message,
)
from pipeline_agent.guardrails import (
    _build_graph_sync_guardrail,
    _guardrail_repair_task,
)
from pipeline_agent.team import build_pipeline_editing_team


@dataclass(frozen=True)
class PipelineEditorTurnResult:
    assistant_message: str
    graph: dict | None
    sync: dict


class PipelineEditorTurnCancelled(Exception):
    def __init__(self, *, rollback_applied: bool, detail: str = "") -> None:
        super().__init__(detail or "Pipeline editor turn cancelled")
        self.rollback_applied = rollback_applied
        self.detail = detail


async def _fetch_graph_safely(
    authorization: str | None,
) -> tuple[dict | None, str | None]:
    try:
        return await fetch_pipeline_graph(authorization=authorization), None
    except Exception as exc:
        print("[pipeline_agent.service] Failed to fetch pipeline graph:", exc)
        return None, str(exc)


async def _run_pipeline_editor_turn(
    *,
    user_message: str,
    canvas_graph: dict | None,
    active_version_uid: str,
    active_version_name: str,
    session_id: str,
    llm_config: LLMConfig,
    authorization: str | None,
    cancellation_state: dict,
) -> PipelineEditorTurnResult:
    """Reconcile, run, validate/repair, persist, and return one agent turn."""
    before_graph, before_error = await _fetch_graph_safely(authorization)
    if before_error:
        raise RuntimeError(
            f"Could not read the persisted pipeline before the agent turn: {before_error}"
        )

    if canvas_graph is not None:
        try:
            await sync_backend_to_canvas_graph(
                canvas_graph,
                active_version_uid,
                active_version_name,
                authorization=authorization,
            )
            before_graph, before_error = await _fetch_graph_safely(authorization)
        except Exception as exc:
            raise RuntimeError(
                "The visible canvas could not be reconciled with the persisted graph; "
                "the agent turn was not started."
            ) from exc
        if before_error:
            raise RuntimeError(
                f"Could not verify the reconciled canvas before the agent turn: {before_error}"
            )

    visible_before_graph = canvas_graph or before_graph
    if isinstance(visible_before_graph, dict):
        cancellation_state["graph"] = deepcopy(visible_before_graph)
    team = build_pipeline_editing_team(
        llm_config=llm_config,
        authorization=authorization,
        provenance_context={"user_query": user_message, "session_id": session_id},
    )
    team_state = load_state_from_disk(session_id)
    if team_state:
        await team.load_state(team_state)

    result = await team.run(
        task=_build_agent_task(user_message, canvas_graph, before_graph)
    )
    assistant_message = _assistant_message_from_result(result)
    after_graph, after_error = await _fetch_graph_safely(authorization)
    sync = _build_graph_sync_guardrail(
        visible_before_graph,
        after_graph,
        user_message,
        after_error,
    )

    if sync["graph_changed"] and not sync["guardrail_passed"] and not after_error:
        repair_result = await team.run(
            task=_guardrail_repair_task(
                user_message,
                canvas_graph,
                after_graph,
                sync.get("validation_errors"),
            )
        )
        repair_message = _assistant_message_from_result(repair_result)
        if repair_message:
            assistant_message = repair_message
        after_graph, repaired_error = await _fetch_graph_safely(authorization)
        sync = _build_graph_sync_guardrail(
            visible_before_graph,
            after_graph,
            user_message,
            repaired_error,
            repaired=True,
        )

    if sync["graph_changed"] and not sync["guardrail_passed"]:
        failure_messages = list(sync.get("validation_errors") or [])
        rollback_error = None
        try:
            if not isinstance(visible_before_graph, dict):
                raise RuntimeError("No pre-turn graph snapshot is available.")
            await sync_backend_to_canvas_graph(
                visible_before_graph,
                active_version_uid,
                active_version_name,
                authorization=authorization,
            )
            after_graph, rollback_fetch_error = await _fetch_graph_safely(authorization)
            if rollback_fetch_error or not isinstance(after_graph, dict):
                raise RuntimeError(
                    rollback_fetch_error or "Rollback graph could not be read."
                )
        except Exception as exc:
            rollback_error = str(exc)
            print("[pipeline_agent.service] Failed to roll back agent graph:", exc)

        rollback_nodes, rollback_edges = _graph_counts(after_graph)
        reason = "; ".join(failure_messages) or sync.get("message") or (
            "No valid graph change was persisted."
        )
        sync.update({
            "status": "rejected",
            "guardrail_passed": False,
            "graph_safe_to_apply": False,
            "rollback_applied": rollback_error is None,
            "node_count": rollback_nodes,
            "edge_count": rollback_edges,
            "updated_at": (
                after_graph.get("updated_at")
                if isinstance(after_graph, dict)
                else None
            ),
            "message": (
                f"The agent result was rejected and the pre-turn pipeline was preserved. {reason}"
                if rollback_error is None
                else f"The agent result was invalid and automatic rollback also failed: {rollback_error}"
            ),
        })
        assistant_message = (
            "I couldn't safely apply that pipeline design, so I preserved the pipeline "
            f"from before this request. Validation details: {reason}"
        )

    if sync["guardrail_passed"] and isinstance(after_graph, dict):
        pipeline = (
            after_graph.get("pipeline")
            if isinstance(after_graph.get("pipeline"), dict)
            else {}
        )
        version_uid = active_version_uid or str(
            pipeline.get("active_version_uid") or "main"
        )
        version_name = active_version_name or str(
            pipeline.get("active_version_name") or pipeline.get("version") or ""
        )
        if version_uid == "main":
            version_name = "Main"
        try:
            await save_active_pipeline_version(
                after_graph,
                version_uid,
                version_name,
                authorization=authorization,
            )
        except Exception as exc:
            print("[pipeline_agent.service] Failed to save active version:", exc)
            sync["message"] = (
                (sync.get("message") or "Agent graph sync completed.")
                + f" Active version save failed: {exc}"
            )
            sync["guardrail_passed"] = False
            sync["graph_safe_to_apply"] = True

    if sync["guardrail_passed"]:
        save_state_to_disk(session_id, await team.save_state())
    else:
        clear_state_from_disk(session_id)

    # Final trust boundary before API serialization. Preserve normal Copilot
    # prose; replace only content that contains a tool envelope, tool result, or
    # persisted-record signature. The replacement is derived from the validated
    # graph and therefore cannot repeat the leaked transcript.
    assistant_message = _safe_assistant_message(assistant_message, after_graph)

    return PipelineEditorTurnResult(assistant_message, after_graph, sync)


async def run_pipeline_editor_turn(
    *,
    user_message: str,
    canvas_graph: dict | None,
    active_version_uid: str,
    active_version_name: str,
    session_id: str,
    llm_config: LLMConfig,
    authorization: str | None,
) -> PipelineEditorTurnResult:
    """Run one agent turn and restore its pre-turn graph if it is cancelled."""
    cancellation_state: dict = {}
    try:
        return await _run_pipeline_editor_turn(
            user_message=user_message,
            canvas_graph=canvas_graph,
            active_version_uid=active_version_uid,
            active_version_name=active_version_name,
            session_id=session_id,
            llm_config=llm_config,
            authorization=authorization,
            cancellation_state=cancellation_state,
        )
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None and hasattr(current_task, "uncancel"):
            current_task.uncancel()
        rollback_graph = cancellation_state.get("graph")
        rollback_applied = not isinstance(rollback_graph, dict)
        rollback_error = ""
        if isinstance(rollback_graph, dict):
            try:
                await sync_backend_to_canvas_graph(
                    rollback_graph,
                    active_version_uid,
                    active_version_name,
                    authorization=authorization,
                )
                rollback_applied = True
            except Exception as exc:
                rollback_error = str(exc)
                print("[pipeline_agent.service] Cancellation rollback failed:", exc)
        clear_state_from_disk(session_id)
        raise PipelineEditorTurnCancelled(
            rollback_applied=rollback_applied,
            detail=rollback_error,
        ) from None
