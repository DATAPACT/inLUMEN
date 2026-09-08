import asyncio
import json
import os
import unittest
import uuid
from unittest.mock import patch

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

    @unittest.skipUnless(os.getenv("RUN_NEO4J_INTEGRATION") == "1", "requires local Neo4j")
    def test_real_two_workspace_pipeline_creation_and_isolation(self):
        # Everything is rolled back, including on assertion/query failures.
        # No LLM calls, production workspaces, or committed fixture data.
        with _base_driver.session() as session:
            tx = session.begin_transaction()
            try:
                async def exercise():
                    workspace = ""

                    async def run_query(query, query_type, **kwargs):
                        _validate_workspace_cypher(query)
                        return json.dumps(tx.run(_scope_cypher(query, workspace)).data(), default=str)

                    with patch("pipeline_agent.tools.run_neo4j_query", side_effect=run_query):
                        for owner in ("alice", "bob"):
                            workspace = f"regression-{owner}-{uuid.uuid4()}"
                            create_step = next(tool for tool in build_pipeline_editor_tools() if tool.__name__ == "create_step")
                            for kind, label in (("source", "Audio upload"), ("task", "Transcription"), ("task", "Sentiment"), ("destination", "JSON output")):
                                await create_step(json.dumps({"type": kind, "label": f"{owner}: {label}"}))
                            rows = tx.run(_scope_cypher("MATCH (p:PIPELINE)-[:HAS_STEP]->(s:STEP) RETURN s.label AS label", workspace)).data()
                            self.assertEqual(len(rows), 4)
                            self.assertTrue(all(row["label"].startswith(owner + ":") for row in rows))
                            edges = tx.run(_scope_cypher("MATCH (s:STEP)-[r:FLOWS_TO]->(t:STEP) RETURN count(r) AS count", workspace)).single()
                            self.assertEqual(edges["count"], 3)
                asyncio.run(exercise())
            finally:
                tx.rollback()
