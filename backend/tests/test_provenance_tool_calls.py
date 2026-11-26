"""Provenance counts agent tool calls, not the graph writes inside them."""
import json
import unittest
from unittest.mock import patch

from graph import neo4j_api


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
            " The call was rejected and the graph was put back as it was.",
        )
        self.assertTrue(json.loads(event["details_json"])["rolled_back"])

    def test_every_agent_tool_call_is_one_event_also_reads_failures_and_repeats(self):
        session = _Session()

        def finish(call_id, tool, status="completed", error=""):
            neo4j_api._finish_agent_tool_call(session, {
                "user_query": "build it", "session_id": "s", "tool": tool, "tool_call_id": call_id,
                "status": status, "error": error, "arguments": '{"params": "{}"}'})

        finish("call-1", "overview")                                   # a read
        finish("call-2", "create_steps", "failed", "steps[0]: bad")    # failed before any write
        finish("call-3", "overview")                                   # the same read again
        write = {"user_query": "build it", "session_id": "s", "tool": "create_steps", "tool_call_id": "call-4"}
        neo4j_api._record_agent_tool_call(session, "create_step", write, [{"step": {}}])
        finish("call-4", "create_steps")                               # a write, then its outcome

        self.assertEqual([event["action"] for event in session.events],
                         ["overview", "create_steps", "overview", "create_steps"])
        self.assertEqual([event["summary"] for event in session.events], [
            "Agent called tool 'overview' once; it made no graph write.",
            "Agent called tool 'create_steps' once; it made no graph write."
            " The call failed: steps[0]: bad",
            "Agent called tool 'overview' once; it made no graph write.",
            "Agent called tool 'create_steps' once; it performed 1 create_step.",
        ])
        last = json.loads(session.events[-1]["details_json"])
        self.assertEqual((last["status"], last["operations"]), ("completed", {"create_step": 1}))

    def test_the_store_reports_each_tool_call_once_with_its_outcome(self):
        import asyncio
        from unittest.mock import AsyncMock

        from pipeline_agent.neo4j_store import Neo4jGraphStore

        store = Neo4jGraphStore(provenance_context={"user_query": "q", "session_id": "s"})
        with patch("pipeline_agent.neo4j_store.run_neo4j_query", new_callable=AsyncMock, return_value="[]") as run:
            async def call():
                with store.tool_call("create_steps"):
                    with store.tool_call("create_steps"):  # the tool's own scope joins the call
                        await store.overview()
                    await store.tool_call_finished("create_steps", {"params": "{}"}, "rejected")
            asyncio.run(call())
        contexts = [c.kwargs["provenance_context"] for c in run.await_args_list]
        self.assertEqual(len({context["tool_call_id"] for context in contexts}), 1)
        self.assertEqual(run.await_args_list[-1].args[1], "tool_call_finished")
        self.assertEqual((contexts[-1]["status"], contexts[-1]["error"]), ("failed", "rejected"))
        self.assertIsNone(store._tool_call)

    def test_an_applied_preview_adds_its_tool_calls_to_the_history_in_order(self):
        session = _Session()
        neo4j_api._record_applied_preview_tool_calls(session, {
            "user_query": "build it", "session_id": "s",
            "calls": [
                {"tool": "create_steps", "operations": {}, "status": "failed", "error": "steps[2]: bad",
                 "arguments": "{}"},
                {"tool": "create_steps", "operations": {"create_pipeline": 1, "create_step": 7},
                 "status": "completed", "arguments": "{}"},
            ],
        })
        self.assertEqual([event["summary"] for event in session.events], [
            "Agent called tool 'create_steps' once; it made no graph write. The call failed: steps[2]: bad"
            " Made in a preview that the user applied.",
            "Agent called tool 'create_steps' once; it performed 1 create_pipeline, 7 create_step."
            " Made in a preview that the user applied.",
        ])
        details = json.loads(session.events[1]["details_json"])
        self.assertTrue(details["from_preview"])
        self.assertEqual(details["user_query"], "build it")
        neo4j_api._record_applied_preview_tool_calls(session, None)  # a proposal without calls
        self.assertEqual(len(session.events), 2)

    def test_a_preview_store_keeps_its_tool_calls_for_the_proposal(self):
        import asyncio
        from unittest.mock import AsyncMock

        from pipeline_agent.neo4j_store import Neo4jGraphStore

        store = Neo4jGraphStore(workspace_id="preview-1", preview_mode=True)

        async def run_query(query, query_type, **kwargs):
            return json.dumps([{"pipeline": {}}]) if query_type == "create_pipeline" else "[]"

        async def turn():
            with store.tool_call("overview"):
                await store.overview()
                await store.tool_call_finished("overview", "{}", None)
            with store.tool_call("create_pipeline"):
                await store.upsert_pipeline("Orders", "Reads orders", "")
                await store.tool_call_finished("create_pipeline", {"name": "Orders"}, None)

        with patch("pipeline_agent.neo4j_store.run_neo4j_query", new_callable=AsyncMock, side_effect=run_query) as run:
            asyncio.run(turn())
        self.assertEqual(store.tool_calls, [
            {"tool": "overview", "operations": {}, "status": "completed", "arguments": "{}"},
            {"tool": "create_pipeline", "operations": {"create_pipeline": 1}, "status": "completed",
             "arguments": '{"name": "Orders"}'},
        ])
        # Nothing about the calls is written to the preview workspace, which is deleted anyway.
        self.assertNotIn("tool_call_finished", [call.args[1] for call in run.await_args_list])

    def test_overlapping_tool_calls_keep_their_own_call(self):
        import asyncio
        from unittest.mock import AsyncMock

        from pipeline_agent.neo4j_store import Neo4jGraphStore

        store = Neo4jGraphStore()
        seen = {}

        async def call(name, first_wait, second_wait):
            with store.tool_call(name):
                await asyncio.sleep(first_wait)
                await store.overview()
                await asyncio.sleep(second_wait)
                await store.overview()

        async def run_both():
            await asyncio.gather(call("a", 0, 0.02), call("b", 0.01, 0.03))

        with patch("pipeline_agent.neo4j_store.run_neo4j_query", new_callable=AsyncMock, return_value="[]") as run:
            asyncio.run(run_both())
        for awaited in run.await_args_list:
            context = awaited.kwargs["provenance_context"]
            seen.setdefault(context["tool"], set()).add(context["tool_call_id"])
        self.assertEqual({tool: len(ids) for tool, ids in seen.items()}, {"a": 1, "b": 1})
        self.assertNotEqual(seen["a"], seen["b"])

    def test_writes_of_every_tool_are_counted(self):
        for query_type in ("configure_subpipeline_step", "create_reusable_pipeline"):
            self.assertTrue(neo4j_api._mutation_query_type(query_type))

if __name__ == "__main__":
    unittest.main()
