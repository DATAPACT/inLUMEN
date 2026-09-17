"""Request-independent lifecycle for one pipeline editor turn."""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from dataclasses import dataclass

from chat_state import clear_state_from_disk, load_state_from_disk, save_state_to_disk
from graph_client import (
    clear_preview_workspace,
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
    is_read_only_pipeline_request,
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
    preview_workspace_id: str | None = None,
) -> PipelineEditorTurnResult:
    """Reconcile, run, validate/repair, persist, and return one agent turn."""
    before_graph, before_error = await _fetch_graph_safely(authorization)
    if before_error:
        raise RuntimeError(
            f"Could not read the persisted pipeline before the agent turn: {before_error}"
        )

    if canvas_graph is not None and preview_workspace_id is None:
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
    agent_backend_graph = before_graph
    if preview_workspace_id is not None:
        if not isinstance(visible_before_graph, dict):
            raise RuntimeError("Could not create a preview without a readable pipeline graph.")
        try:
            # Preview turns start from the visible design in an isolated
            # workspace; they never reconcile the browser graph into live data.
            await sync_backend_to_canvas_graph(
                visible_before_graph,
                "main",
                "Main",
                authorization=authorization,
                workspace_id=preview_workspace_id,
            )
        except Exception as exc:
            raise RuntimeError("Could not prepare an isolated workspace for the graph preview.") from exc
        try:
            # Give the agent the persisted staged snapshot as its backend
            # context. The browser snapshot is intentionally kept separate
            # below so follow-up edits can compare the UI and graph state.
            agent_backend_graph = await fetch_pipeline_graph(
                authorization=authorization,
                workspace_id=preview_workspace_id,
            )
        except Exception as exc:
            raise RuntimeError("Could not verify the isolated graph before the preview turn.") from exc

    if isinstance(visible_before_graph, dict) and preview_workspace_id is None:
        cancellation_state["graph"] = deepcopy(visible_before_graph)
    team = build_pipeline_editing_team(
        llm_config=llm_config,
        authorization=authorization,
        provenance_context={"user_query": user_message, "session_id": session_id},
        workspace_id=preview_workspace_id,
        preview_mode=preview_workspace_id is not None,
    )
    team_state = load_state_from_disk(session_id)
    if team_state:
        await team.load_state(team_state)

    result = await team.run(
        task=_build_agent_task(user_message, canvas_graph, agent_backend_graph)
    )
    assistant_message = _assistant_message_from_result(result)
    try:
        after_graph = await fetch_pipeline_graph(
            authorization=authorization,
            **({"workspace_id": preview_workspace_id} if preview_workspace_id else {}),
        )
        after_error = None
    except Exception as exc:
        after_graph, after_error = None, str(exc)
    sync = _build_graph_sync_guardrail(
        visible_before_graph,
        after_graph,
        user_message,
        after_error,
    )

    # Informational requests must never turn model paraphrasing or an accidental
    # tool write into a real edit or a Review AI proposal. Preview workspaces can
    # simply be discarded; direct turns restore the visible graph before any
    # active-version save occurs below.
    if is_read_only_pipeline_request(user_message) and sync.get("graph_changed"):
        if preview_workspace_id is None:
            if not isinstance(visible_before_graph, dict):
                raise RuntimeError("No pre-turn graph snapshot is available for a read-only request.")
            try:
                await sync_backend_to_canvas_graph(
                    visible_before_graph,
                    active_version_uid,
                    active_version_name,
                    authorization=authorization,
                )
                after_graph = deepcopy(visible_before_graph)
                after_error = None
            except Exception as exc:
                after_error = str(exc)
        else:
            after_graph = deepcopy(visible_before_graph)
            after_error = None
        sync = _build_graph_sync_guardrail(
            visible_before_graph,
            after_graph,
            user_message,
            after_error,
        )

    if (
        not sync["guardrail_passed"]
        and sync.get("status") == "invalid"
        and not after_error
    ):
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
        try:
            after_graph = await fetch_pipeline_graph(
                authorization=authorization,
                **({"workspace_id": preview_workspace_id} if preview_workspace_id else {}),
            )
            repaired_error = None
        except Exception as exc:
            after_graph, repaired_error = None, str(exc)
        sync = _build_graph_sync_guardrail(
            visible_before_graph,
            after_graph,
            user_message,
            repaired_error,
            repaired=True,
        )

    if not sync["guardrail_passed"] and sync.get("status") == "invalid":
        failure_messages = list(sync.get("validation_errors") or [])
        rollback_error = None
        if sync["graph_changed"] and preview_workspace_id is None:
            try:
                if not isinstance(visible_before_graph, dict):
                    raise RuntimeError("No pre-turn graph snapshot is available.")
                await sync_backend_to_canvas_graph(
                    visible_before_graph,
                    active_version_uid,
                    active_version_name,
                    authorization=authorization,
                )
                after_graph, rollback_fetch_error = await _fetch_graph_safely(
                    authorization
                )
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

    if sync["guardrail_passed"] and isinstance(after_graph, dict) and preview_workspace_id is None:
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

    # Until the user accepts a proposal, its tool history is not an accurate
    # reflection of the live graph. Keep the prior chat state unchanged.
    if preview_workspace_id is None:
        if sync["guardrail_passed"]:
            save_state_to_disk(session_id, await team.save_state())
        else:
            clear_state_from_disk(session_id)

    # Final trust boundary before API serialization. Preserve normal Copilot
    # prose; replace only content that contains a tool envelope, tool result, or
    # persisted-record signature. The replacement is derived from the validated
    # graph and therefore cannot repeat the leaked transcript.
    if preview_workspace_id is not None and isinstance(after_graph, dict):
        after_graph = deepcopy(after_graph)
        pipeline = after_graph.get("pipeline")
        if isinstance(pipeline, dict):
            pipeline["active_version_uid"] = active_version_uid or "main"
            pipeline["active_version_name"] = active_version_name or "Main"
            pipeline["version"] = active_version_name or "Main"
        if sync["guardrail_passed"] and sync.get("graph_changed"):
            sync["status"] = "preview"
            sync["preview_pending"] = True
            sync["message"] = "Review the proposed graph. Your saved pipeline is unchanged until you apply it."
        elif sync["guardrail_passed"]:
            sync["message"] = "No graph changes were made. Your saved pipeline is unchanged."

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
    preview_changes: bool = False,
) -> PipelineEditorTurnResult:
    """Run one agent turn, isolating previews and restoring live turns on cancel."""
    cancellation_state: dict = {}
    preview_workspace_id = str(uuid.uuid4()) if preview_changes else None
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
            preview_workspace_id=preview_workspace_id,
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
        if preview_workspace_id is None:
            clear_state_from_disk(session_id)
        raise PipelineEditorTurnCancelled(
            rollback_applied=True if preview_workspace_id is not None else rollback_applied,
            detail=rollback_error,
        ) from None
    finally:
        if preview_workspace_id is not None:
            try:
                await asyncio.shield(clear_preview_workspace(
                    preview_workspace_id,
                    authorization=authorization,
                ))
            except Exception as exc:
                print("[pipeline_agent.service] Failed to clean up preview workspace:", exc)
