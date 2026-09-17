import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_config import LLMConfig  # noqa: E402
from pipeline_agent.service import (  # noqa: E402
    PipelineEditorTurnCancelled,
    run_pipeline_editor_turn,
)


class PipelineAgentServiceTest(unittest.TestCase):
    @patch("pipeline_agent.service.clear_state_from_disk")
    @patch("pipeline_agent.service.load_state_from_disk", return_value=None)
    @patch("pipeline_agent.service.fetch_pipeline_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.sync_backend_to_canvas_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.build_pipeline_editing_team")
    def test_cancelled_turn_restores_the_visible_pre_turn_graph(
        self,
        build_team,
        sync_graph,
        fetch_graph,
        _load_state,
        clear_state,
    ):
        visible_graph = {
            "updated_at": "2026-08-11T10:00:00Z",
            "nodes": [{"id": "source", "data": {"type": "source"}}],
            "edges": [],
        }
        fetch_graph.side_effect = [visible_graph, visible_graph]
        team = MagicMock()
        team.run = AsyncMock(side_effect=asyncio.CancelledError())
        build_team.return_value = team

        with self.assertRaises(PipelineEditorTurnCancelled) as raised:
            asyncio.run(run_pipeline_editor_turn(
                user_message="Build a pipeline",
                canvas_graph=visible_graph,
                active_version_uid="main",
                active_version_name="Main",
                session_id="cancel-session",
                llm_config=LLMConfig(
                    provider="openrouter",
                    model="test/model",
                    base_url="https://example.test/v1",
                    api_key="secret",
                ),
                authorization="Bearer token",
            ))

        self.assertTrue(raised.exception.rollback_applied)
        self.assertEqual(2, sync_graph.await_count)
        self.assertEqual(visible_graph, sync_graph.await_args_list[-1].args[0])
        clear_state.assert_called_once_with("cancel-session")

    @patch("pipeline_agent.service.save_state_to_disk")
    @patch("pipeline_agent.service.load_state_from_disk", return_value=None)
    @patch("pipeline_agent.service.save_active_pipeline_version", new_callable=AsyncMock)
    @patch("pipeline_agent.service.fetch_pipeline_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.build_pipeline_editing_team")
    def test_mutation_turn_preserves_safe_copilot_prose_and_saves_state(
        self,
        build_team,
        fetch_graph,
        save_version,
        _load_state,
        save_state,
    ):
        before_graph = {
            "updated_at": "2026-08-11T09:59:00Z",
            "pipeline": {"active_version_uid": "main", "version": "Main"},
            "nodes": [],
            "edges": [],
        }
        graph = {
            "updated_at": "2026-08-11T10:00:00Z",
            "pipeline": {
                "label": "Safe Pipeline",
                "active_version_uid": "main",
                "version": "Main",
            },
            "nodes": [
                {"id": "1", "data": {"type": "source", "label": "Input"}},
                {"id": "2", "data": {"type": "destination", "label": "Output"}},
            ],
            "edges": [{
                "source": "1",
                "target": "2",
                "sourceHandle": "data",
                "targetHandle": "data",
            }],
        }
        fetch_graph.side_effect = [before_graph, graph]
        team = MagicMock()
        team.run = AsyncMock(return_value=SimpleNamespace(messages=[
            SimpleNamespace(
                type="TextMessage",
                source="assistant",
                content="Model-authored explanation that must not cross the mutation boundary.",
            ),
        ]))
        team.save_state = AsyncMock(return_value={"history": []})
        build_team.return_value = team

        result = asyncio.run(run_pipeline_editor_turn(
            user_message="Create a safe pipeline",
            canvas_graph=None,
            active_version_uid="main",
            active_version_name="Main",
            session_id="session-1",
            llm_config=LLMConfig(
                provider="openrouter",
                model="test/model",
                base_url="https://example.test/v1",
                api_key="secret",
            ),
            authorization="Bearer token",
        ))

        self.assertIn("Model-authored explanation", result.assistant_message)
        self.assertEqual("synced", result.sync["status"])
        self.assertTrue(result.sync["guardrail_passed"])
        self.assertEqual(2, fetch_graph.await_count)
        team.run.assert_awaited_once()
        save_version.assert_awaited_once_with(
            graph,
            "main",
            "Main",
            authorization="Bearer token",
        )
        save_state.assert_called_once_with("session-1", {"history": []})

    @patch("pipeline_agent.service.save_state_to_disk")
    @patch("pipeline_agent.service.load_state_from_disk", return_value=None)
    @patch("pipeline_agent.service.save_active_pipeline_version", new_callable=AsyncMock)
    @patch("pipeline_agent.service.fetch_pipeline_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.sync_backend_to_canvas_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.build_pipeline_editing_team")
    def test_direct_follow_up_turn_keeps_reconciled_backend_context_separate_from_canvas(
        self,
        build_team,
        sync_graph,
        fetch_graph,
        save_version,
        _load_state,
        _save_state,
    ):
        canvas_graph = {
            "pipeline": {"label": "Visible canvas label"},
            "nodes": [{"id": "source", "data": {"type": "source", "label": "Canvas input"}}],
            "edges": [],
        }
        reconciled_graph = {
            "pipeline": {
                "label": "Persisted backend label",
                "description": "Enriched backend context",
            },
            "nodes": [{"id": "source", "data": {"type": "source", "label": "Backend input"}}],
            "edges": [],
        }
        proposed_graph = {
            **reconciled_graph,
            "nodes": [
                *reconciled_graph["nodes"],
                {"id": "destination", "data": {"type": "destination", "label": "Output"}},
            ],
            "edges": [{
                "source": "source",
                "target": "destination",
                "sourceHandle": "data",
                "targetHandle": "data",
            }],
        }
        fetch_graph.side_effect = [reconciled_graph, reconciled_graph, proposed_graph]
        team = MagicMock()
        team.run = AsyncMock(return_value=SimpleNamespace(messages=[SimpleNamespace(
            type="TextMessage",
            source="assistant",
            content="Added the output.",
        )]))
        team.save_state = AsyncMock(return_value={"history": []})
        build_team.return_value = team

        asyncio.run(run_pipeline_editor_turn(
            user_message="Add an output",
            canvas_graph=canvas_graph,
            active_version_uid="main",
            active_version_name="Main",
            session_id="direct-follow-up-session",
            llm_config=LLMConfig(
                provider="openrouter",
                model="test/model",
                base_url="https://example.test/v1",
                api_key="secret",
            ),
            authorization="Bearer token",
        ))

        task = team.run.await_args.kwargs["task"]
        self.assertIn('"description": "Enriched backend context"', task)
        self.assertIn('"label": "Canvas input"', task)
        self.assertIn('"label": "Backend input"', task)
        self.assertEqual(1, sync_graph.await_count)
        save_version.assert_awaited_once()

    @patch("pipeline_agent.service.clear_preview_workspace", new_callable=AsyncMock)
    @patch("pipeline_agent.service.save_state_to_disk")
    @patch("pipeline_agent.service.load_state_from_disk", return_value=None)
    @patch("pipeline_agent.service.save_active_pipeline_version", new_callable=AsyncMock)
    @patch("pipeline_agent.service.fetch_pipeline_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.sync_backend_to_canvas_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service._build_graph_sync_guardrail")
    @patch("pipeline_agent.service.build_pipeline_editing_team")
    def test_preview_turn_uses_and_cleans_an_isolated_workspace_without_persisting_live_graph(
        self,
        build_team,
        guardrail,
        sync_graph,
        fetch_graph,
        save_version,
        _load_state,
        save_state,
        clear_preview,
    ):
        visible_graph = {
            "pipeline": {"active_version_uid": "version-7", "version": "Review"},
            "nodes": [{"id": "source", "data": {"type": "source", "label": "Input"}}],
            "edges": [],
        }
        staged_graph = {
            **visible_graph,
            "pipeline": {
                **visible_graph["pipeline"],
                "description": "Persisted isolated stage context",
            },
        }
        proposed_graph = {
            "pipeline": {"active_version_uid": "main", "version": "Main"},
            "nodes": [
                {"id": "source", "data": {"type": "source", "label": "Input"}},
                {"id": "destination", "data": {"type": "destination", "label": "Output"}},
            ],
            "edges": [{"source": "source", "target": "destination"}],
        }
        fetch_graph.side_effect = [visible_graph, staged_graph, proposed_graph]
        guardrail.return_value = {
            "status": "synced",
            "guardrail_passed": True,
            "graph_safe_to_apply": True,
            "graph_changed": True,
            "node_count": 2,
            "edge_count": 1,
            "updated_at": None,
            "message": "Validated proposal.",
            "validation_errors": [],
        }
        team = MagicMock()
        team.run = AsyncMock(return_value=SimpleNamespace(messages=[SimpleNamespace(
            type="TextMessage",
            source="assistant",
            content="Proposed a connected output.",
        )]))
        team.save_state = AsyncMock(return_value={"history": []})
        build_team.return_value = team

        result = asyncio.run(run_pipeline_editor_turn(
            user_message="Add an output",
            canvas_graph=visible_graph,
            active_version_uid="version-7",
            active_version_name="Review",
            session_id="preview-session",
            llm_config=LLMConfig(
                provider="openrouter",
                model="test/model",
                base_url="https://example.test/v1",
                api_key="secret",
            ),
            authorization="Bearer token",
            preview_changes=True,
        ))

        stage_workspace_id = build_team.call_args.kwargs["workspace_id"]
        self.assertTrue(stage_workspace_id)
        self.assertEqual("version-7", result.graph["pipeline"]["active_version_uid"])
        self.assertEqual("Review", result.graph["pipeline"]["version"])
        self.assertEqual("preview", result.sync["status"])
        self.assertTrue(result.sync["preview_pending"])
        self.assertIn('"description": "Persisted isolated stage context"', team.run.await_args.kwargs["task"])
        self.assertEqual(3, fetch_graph.await_count)
        self.assertEqual(1, sync_graph.await_count)
        self.assertEqual(stage_workspace_id, sync_graph.await_args.kwargs["workspace_id"])
        save_version.assert_not_awaited()
        save_state.assert_not_called()
        clear_preview.assert_awaited_once_with(stage_workspace_id, authorization="Bearer token")

    @patch("pipeline_agent.service.clear_preview_workspace", new_callable=AsyncMock)
    @patch("pipeline_agent.service.clear_state_from_disk")
    @patch("pipeline_agent.service.load_state_from_disk", return_value=None)
    @patch("pipeline_agent.service.fetch_pipeline_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.sync_backend_to_canvas_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.build_pipeline_editing_team")
    def test_cancelled_preview_cleans_stage_without_restoring_or_clearing_live_state(
        self,
        build_team,
        sync_graph,
        fetch_graph,
        _load_state,
        clear_state,
        clear_preview,
    ):
        visible_graph = {"nodes": [], "edges": []}
        fetch_graph.return_value = visible_graph
        team = MagicMock()
        team.run = AsyncMock(side_effect=asyncio.CancelledError())
        build_team.return_value = team

        with self.assertRaises(PipelineEditorTurnCancelled) as raised:
            asyncio.run(run_pipeline_editor_turn(
                user_message="Create a pipeline",
                canvas_graph=visible_graph,
                active_version_uid="main",
                active_version_name="Main",
                session_id="preview-cancel-session",
                llm_config=LLMConfig(
                    provider="openrouter",
                    model="test/model",
                    base_url="https://example.test/v1",
                    api_key="secret",
                ),
                authorization="Bearer token",
                preview_changes=True,
            ))

        self.assertTrue(raised.exception.rollback_applied)
        self.assertEqual(1, sync_graph.await_count)
        self.assertIsNotNone(sync_graph.await_args.kwargs["workspace_id"])
        clear_state.assert_not_called()
        clear_preview.assert_awaited_once_with(
            build_team.call_args.kwargs["workspace_id"],
            authorization="Bearer token",
        )

    @patch("pipeline_agent.service.clear_state_from_disk")
    @patch("pipeline_agent.service.save_state_to_disk")
    @patch("pipeline_agent.service.load_state_from_disk", return_value=None)
    @patch("pipeline_agent.service.save_active_pipeline_version", new_callable=AsyncMock)
    @patch("pipeline_agent.service.fetch_pipeline_graph", new_callable=AsyncMock)
    @patch("pipeline_agent.service.build_pipeline_editing_team")
    def test_unchanged_incomplete_topology_triggers_repair(
        self,
        build_team,
        fetch_graph,
        save_version,
        _load_state,
        save_state,
        clear_state,
    ):
        def node(node_id, kind, label, *, template="", parameters=None):
            data = {"type": kind, "label": label, "description": label}
            if template:
                data["template_label"] = template
            if parameters is not None:
                data["param"] = parameters
            return {"id": str(node_id), "data": data}

        def edge(source, target, source_port, target_port):
            return {
                "source": str(source),
                "target": str(target),
                "sourceHandle": source_port,
                "targetHandle": target_port,
            }

        pipeline = {
            "label": "Row Validation",
            "active_version_uid": "main",
            "version": "Main",
        }
        partial_graph = {
            "updated_at": "2026-09-01T10:00:00Z",
            "pipeline": pipeline,
            "nodes": [
                node(1, "source", "Input"),
                node(
                    2,
                    "flow",
                    "Valid Row?",
                    template="Condition",
                    parameters={"expression": "value.is_valid == true"},
                ),
                node(3, "destination", "Clean CSV"),
            ],
            "edges": [
                edge(1, 2, "data", "value"),
                edge(2, 3, "when_true", "data"),
            ],
        }
        complete_graph = {
            **partial_graph,
            "updated_at": "2026-09-01T10:01:00Z",
            "nodes": [
                *partial_graph["nodes"],
                node(4, "destination", "Outlier CSV"),
            ],
            "edges": [
                *partial_graph["edges"],
                edge(2, 4, "when_false", "data"),
            ],
        }
        fetch_graph.side_effect = [partial_graph, partial_graph, complete_graph]
        team = MagicMock()
        team.run = AsyncMock(side_effect=[
            SimpleNamespace(messages=[SimpleNamespace(
                type="TextMessage",
                source="assistant",
                content="The existing pipeline is complete.",
            )]),
            SimpleNamespace(messages=[SimpleNamespace(
                type="TextMessage",
                source="assistant",
                content="Added the missing outlier branch.",
            )]),
        ])
        team.save_state = AsyncMock(return_value={"history": []})
        build_team.return_value = team

        result = asyncio.run(run_pipeline_editor_turn(
            user_message=(
                "Split into two branches: a clean-data branch saved to CSV and an "
                "outlier branch saved to a separate CSV."
            ),
            canvas_graph=None,
            active_version_uid="main",
            active_version_name="Main",
            session_id="repair-session",
            llm_config=LLMConfig(
                provider="openrouter",
                model="test/model",
                base_url="https://example.test/v1",
                api_key="secret",
            ),
            authorization="Bearer token",
        ))

        self.assertEqual(2, team.run.await_count)
        self.assertEqual("synced", result.sync["status"])
        self.assertTrue(result.sync["repaired"])
        self.assertIn("Added the missing outlier branch", result.assistant_message)
        save_version.assert_awaited_once_with(
            complete_graph,
            "main",
            "Main",
            authorization="Bearer token",
        )
        save_state.assert_called_once_with("repair-session", {"history": []})
        clear_state.assert_not_called()

if __name__ == "__main__":
    unittest.main()
