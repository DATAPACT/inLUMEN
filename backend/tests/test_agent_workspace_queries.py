import asyncio
import json
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from neo4j_api import _base_driver, _scope_cypher, _validate_workspace_cypher
from pipeline_agent.tools import build_pipeline_editor_tools


class AgentWorkspaceQueryTests(unittest.TestCase):
    def test_generated_create_step_passes_the_real_workspace_validator(self):
        captured = []

        async def run_query(query, query_type, **kwargs):
            _validate_workspace_cypher(query)
            captured.append(query)
            return json.dumps([{"step": {"flow_id": "1"}}])

        with patch("pipeline_agent.tools.run_neo4j_query", side_effect=run_query):
            create_step = next(tool for tool in build_pipeline_editor_tools() if tool.__name__ == "create_step")
            asyncio.run(create_step(json.dumps({"type": "source", "label": "Audio upload"})))
        self.assertTrue(captured)
        self.assertIn("[:FLOWS_TO]->(:STEP)", captured[-1])
        self.assertNotEqual(_scope_cypher(captured[-1], "alice"), _scope_cypher(captured[-1], "bob"))

    def test_explicit_predecessor_and_clear_pass_workspace_validation(self):
        captured = []

        async def run_query(query, query_type, **kwargs):
            _validate_workspace_cypher(query)
            captured.append(query_type)
            if query_type == "resolve_step_predecessor":
                return json.dumps([{"predecessor": {
                    "pipeline_uid": "design", "flow_id": "2", "type": "task",
                    "primary_output_port": "output",
                }}])
            return json.dumps([{"step": {"flow_id": "3"}}])

        tools = {tool.__name__: tool for tool in build_pipeline_editor_tools()}
        with patch("pipeline_agent.tools.run_neo4j_query", side_effect=run_query):
            asyncio.run(tools["create_step"](json.dumps({
                "type": "destination", "label": "Results", "after_flow_id": "2",
            })))
            asyncio.run(tools["delete_all_steps"]("{}"))
        self.assertEqual(captured, ["resolve_step_predecessor", "create_step", "delete_all_steps"])

    def test_preview_uses_existing_reusable_catalog_but_rejects_new_catalog_writes(self):
        run_query = AsyncMock(return_value="[]")
        tools = build_pipeline_editor_tools(
            workspace_id="preview-workspace",
            preview_mode=True,
        )
        list_reusable = next(tool for tool in tools if tool.__name__ == "list_reusable_pipelines")
        with patch("pipeline_agent.tools.run_neo4j_query", run_query):
            asyncio.run(list_reusable("{}"))
            self.assertEqual("list_reusable_pipelines", run_query.await_args.args[1])
            self.assertIsNone(run_query.await_args.kwargs["workspace_id"])

            run_query.reset_mock()
            create_reusable = next(tool for tool in tools if tool.__name__ == "create_reusable_pipeline")
            with patch("pipeline_agent.tools.validate_pipeline_graph", return_value={"valid": True}), \
                 patch("pipeline_agent.tools.derive_subpipeline_interface", return_value={"inputs": [{}], "outputs": [{}]}), \
                 patch("pipeline_agent.tools.public_ports_for_interface", return_value={"inputs": [{}], "outputs": [{}]}):
                with self.assertRaisesRegex(RuntimeError, "cannot be included in a graph preview"):
                    asyncio.run(create_reusable(json.dumps({
                        "name": "Preview reusable",
                        "graph": {"nodes": [], "edges": []},
                    })))

            run_query.assert_awaited_once()
            self.assertEqual("find_reusable_pipeline_by_name", run_query.await_args.args[1])
            self.assertIsNone(run_query.await_args.kwargs["workspace_id"])

    @unittest.skipUnless(os.getenv("RUN_NEO4J_INTEGRATION") == "1", "requires local Neo4j")
    def test_real_two_workspace_pipeline_creation_and_isolation(self):
        # Everything is rolled back, including on assertion/query failures.
        # No LLM calls, production workspaces, or committed fixture data.
        with _base_driver.session() as session:
            tx = session.begin_transaction()
            try:
                async def exercise():
                    workspace = ""
                    created_workspaces = []

                    async def run_query(query, query_type, **kwargs):
                        _validate_workspace_cypher(query)
                        return json.dumps(tx.run(_scope_cypher(query, workspace)).data(), default=str)

                    with patch("pipeline_agent.tools.run_neo4j_query", side_effect=run_query):
                        for owner in ("alice", "bob"):
                            workspace = f"regression-{owner}-{uuid.uuid4()}"
                            created_workspaces.append(workspace)
                            create_step = next(tool for tool in build_pipeline_editor_tools() if tool.__name__ == "create_step")
                            for index, (kind, label) in enumerate((("source", "Audio upload"), ("task", "Transcription"), ("task", "Sentiment"), ("destination", "JSON output"))):
                                await create_step(json.dumps({
                                    "type": kind, "label": f"{owner}: {label}",
                                    **({"after_flow_id": str(index)} if index else {}),
                                }))
                            rows = tx.run(_scope_cypher("MATCH (p:PIPELINE)-[:HAS_STEP]->(s:STEP) RETURN s.label AS label", workspace)).data()
                            self.assertEqual(len(rows), 4)
                            self.assertTrue(all(row["label"].startswith(owner + ":") for row in rows))
                            edges = tx.run(_scope_cypher("MATCH (s:STEP)-[r:FLOWS_TO]->(t:STEP) RETURN count(r) AS count", workspace)).single()
                            self.assertEqual(edges["count"], 3)
                            tools = {tool.__name__: tool for tool in build_pipeline_editor_tools()}
                            for index, label in enumerate(("Entity Recognition", "Anonymization")):
                                await tools["insert_step"](json.dumps({
                                    "type": "task", "label": f"{owner}: {label}",
                                    "after_flow_id": "2" if index == 0 else "5",
                                    "before_flow_id": "3",
                                }))
                            rows = tx.run(_scope_cypher("MATCH (s:STEP) RETURN count(s) AS count", workspace)).single()
                            self.assertEqual(rows["count"], 6)
                            edges = tx.run(_scope_cypher("MATCH (s:STEP)-[r:FLOWS_TO]->(t:STEP) RETURN count(r) AS count", workspace)).single()
                            self.assertEqual(edges["count"], 5)
                        await tools["delete_all_steps"]("{}")
                        self.assertEqual(tx.run(_scope_cypher("MATCH (s:STEP) RETURN count(s) AS count", workspace)).single()["count"], 0)
                        alice_workspace = tx.run(_scope_cypher("MATCH (s:STEP) RETURN count(s) AS count", created_workspaces[0])).single()
                        self.assertEqual(alice_workspace["count"], 6)
                asyncio.run(exercise())
            finally:
                tx.rollback()
