"""Provenance counts agent tool calls, not the graph writes inside them."""
import json
import unittest
from unittest.mock import patch

import neo4j_api


class _Result:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record


class _Session:
    """Keeps PROVENANCE_EVENT nodes in a list."""

    def __init__(self):
        self.events = []

    def run(self, query, **params):
        if "CREATE (event:PROVENANCE_EVENT" in query:
            self.events.append({
                "uid": f"event-{len(self.events) + 1}", "pipeline_uid": params["pipeline_uid"],
                "action": params["action"], "summary": params["summary"],
                "details_json": params["details_json"], "tool_call_id": params["tool_call_id"],
            })
            return _Result(None)
        if "MATCH (event:PROVENANCE_EVENT {tool_call_id" in query:
            return _Result(next(
                (e for e in self.events if e["tool_call_id"] == params["tool_call_id"]), None))
        if "MATCH (event:PROVENANCE_EVENT {uid" in query:
            event = next(e for e in self.events if e["uid"] == params["uid"])
            event.update(summary=params["summary"], details_json=params["details_json"])
            return _Result(None)
        raise AssertionError(query)


class ProvenanceToolCallTests(unittest.TestCase):
    def setUp(self):
        version = {"pipeline_uid": "p1", "version_uid": "main", "version_name": "Main"}
        for name, value in (("_active_pipeline_version", version), ("_provenance_graph_snapshot", {"nodes": []})):
            patcher = patch.object(neo4j_api, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_one_tool_call_with_many_writes_is_one_event(self):
        session = _Session()
        context = {"user_query": "build it", "session_id": "s", "tool": "create_steps", "tool_call_id": "call-1"}
        for _ in range(7):
            neo4j_api._record_agent_tool_call(session, "create_step", context, [{"step": {}}])
        neo4j_api._record_agent_tool_call(session, "connect_steps", context, [{"connection": {}}])

        self.assertEqual(len(session.events), 1)
        event = session.events[0]
        self.assertEqual(event["action"], "create_steps")
        self.assertEqual(
            event["summary"],
            "Agent called tool 'create_steps' once; it performed 7 create_step, 1 connect_steps.",
        )
        details = json.loads(event["details_json"])
        self.assertEqual(details["operations"], {"create_step": 7, "connect_steps": 1})
        self.assertEqual(details["tool_calls"], 1)
        self.assertNotIn("query_type", details)

    def test_a_write_outside_a_grouped_tool_call_keeps_its_own_event(self):
        session = _Session()
        context = {"user_query": "add one", "session_id": "s", "tool": "", "tool_call_id": ""}
        neo4j_api._record_agent_tool_call(session, "create_step", context, [])
        neo4j_api._record_agent_tool_call(session, "create_step", context, [])

        self.assertEqual([e["action"] for e in session.events], ["create_step", "create_step"])
        self.assertEqual(session.events[0]["summary"], "Agent executed pipeline operation 'create_step'.")
        self.assertEqual(json.loads(session.events[0]["details_json"])["query_type"], "create_step")


    def test_a_rolled_back_tool_call_says_so_and_empty_writes_are_not_counted(self):
        session = _Session()
        context = {"user_query": "build it", "session_id": "s", "tool": "create_steps", "tool_call_id": "call-2"}
        neo4j_api._record_agent_tool_call(session, "create_step", context, [{"step": {}}])
        neo4j_api._record_agent_tool_call(session, "create_step", context, [{"step": {}}])
        neo4j_api._record_agent_tool_call(session, "create_step", context, [])  # rejected: matched nothing
        neo4j_api._mark_agent_tool_call_rolled_back(session, context)

        self.assertEqual(len(session.events), 1)
        event = session.events[0]
        self.assertEqual(
            event["summary"],
            "Agent called tool 'create_steps' once; it performed 2 create_step."
            " The call was rejected and these changes were rolled back.",
        )
        self.assertTrue(json.loads(event["details_json"])["rolled_back"])

    def test_the_agent_store_writes_to_the_version_being_edited(self):
        from llm_config import LLMConfig
        from pipeline_agent.team import build_pipeline_editing_team

        config = LLMConfig(provider="openrouter", model="test/model", base_url="https://example.test/v1", api_key="k")
        with patch("pipeline_agent.team.select_model_client"), \
                patch("pipeline_agent.team.build_design_team") as build_design_team:
            build_pipeline_editing_team(config, active_version_uid="v2", active_version_name="Second")
        store = build_design_team.call_args.args[1]
        self.assertEqual((store.active_version_uid, store.active_version_name), ("v2", "Second"))


if __name__ == "__main__":
    unittest.main()
