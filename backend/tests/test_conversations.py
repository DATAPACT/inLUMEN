import asyncio
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text

import analytics_api
from conversations import ConversationError, ConversationStore, registry_turn_id
from pipeline_agent.service import PipelineEditorTurnResult


ALICE = {"user_id": "alice", "workspace_id": "workspace-a"}
BOB = {"user_id": "bob", "workspace_id": "workspace-b"}


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.location = str(Path(self.directory.name) / "chat.sqlite3")
        self.store = ConversationStore(self.location)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def turn(self, turn="turn-first", scope=ALICE, pending=False):
        cid, _, _ = self.store.start(scope, turn_id=turn, request_sha256=turn, user_message="Design my pipeline")
        response = {"session_id": cid, "assistant_message": "Pipeline designed", "sync": {"preview_pending": True}}
        if not pending:
            self.store.finish(scope, turn, response)
        return cid, response

    def test_cross_browser_and_restart_recovery_is_private_and_idempotent(self):
        cid, _ = self.turn(pending=True)
        self.assertEqual(self.store.read(ALICE)["conversation"]["pending_turn_id"], "turn-first")
        with self.assertRaises(ConversationError) as error:
            self.store.start(ALICE, turn_id="turn-first", request_sha256="turn-first", user_message="Design my pipeline")
        self.assertEqual(error.exception.code, "turn_pending")
        result = {"session_id": cid, "assistant_message": "Done", "graph": {"nodes": []}}
        self.store.finish(ALICE, "turn-first", result)
        reopened = ConversationStore(self.location)
        try:
            page = reopened.read(ALICE)
            self.assertEqual([m["content"] for m in page["messages"]], ["Design my pipeline", "Done"])
            self.assertIsNone(reopened.read(BOB)["conversation"])
            self.assertIsNone(reopened.read({**ALICE, "user_id": "other-member"})["conversation"])
            _, replay, status = reopened.start(ALICE, turn_id="turn-first", request_sha256="turn-first", user_message="Design my pipeline")
            self.assertEqual(status, 200)
            self.assertEqual(replay, result)
            with self.assertRaises(ConversationError):
                reopened.start(ALICE, turn_id="turn-first", request_sha256="changed", user_message="Another request")
            with self.assertRaises(ConversationError):
                reopened.start(BOB, turn_id="turn-new", request_sha256="new", user_message="Steal history", conversation_id=cid)
            with self.assertRaises(ConversationError) as error:
                reopened.update_proposal(BOB, result["assistant_message_id"], "applied")
            self.assertEqual(error.exception.status, 404)
        finally:
            reopened.engine.dispose()

    def test_reset_refuses_pending_then_propagates_and_erases_response_content(self):
        cid, _ = self.turn(pending=True)
        with self.assertRaises(ConversationError):
            self.store.reset(ALICE)
        with self.assertRaises(ConversationError):
            with self.store.clear_guard(ALICE["workspace_id"]):
                self.fail("Pending turns must block reset")
        self.store.finish(ALICE, "turn-first", {"assistant_message": "private output", "graph": {"secret": "old contents"}})
        revision = self.store.read(ALICE)["conversation"]["revision"]
        self.store.reset(ALICE, cid)
        page = self.store.read(ALICE, after=revision)
        self.assertTrue(page["reset"])
        self.assertEqual(page["messages"], [])
        _, replay, status = self.store.start(ALICE, turn_id="turn-first", request_sha256="turn-first", user_message="Design my pipeline")
        self.assertEqual(status, 409)
        self.assertNotIn("private output", str(replay))
        self.assertNotIn("old contents", str(replay))
        self.turn("turn-new")
        self.turn("bob-turn", BOB)
        with self.store.clear_guard(ALICE["workspace_id"]) as connection:
            self.store.clear_locked(connection, ALICE["workspace_id"])
        self.assertIsNone(self.store.read(ALICE)["conversation"])
        self.assertEqual(len(self.store.read(BOB)["messages"]), 2)

    def test_cancel_is_scoped_and_visible_to_another_worker(self):
        self.turn(pending=True)
        worker = ConversationStore(self.location)
        try:
            worker.cancel(BOB, "turn-first")
            self.assertFalse(self.store.heartbeat(ALICE, "turn-first"))
            self.assertNotEqual(registry_turn_id(ALICE, "turn-first"), registry_turn_id(BOB, "turn-first"))
            worker.cancel(ALICE, "turn-first")
            self.assertTrue(self.store.heartbeat(ALICE, "turn-first"))
        finally:
            worker.engine.dispose()

    def test_cursor_pagination_and_updates_do_not_duplicate_messages(self):
        for i in range(60):
            self.turn(f"turn-{i}")
        page = self.store.read(ALICE)
        self.assertEqual(len(page["messages"]), 100)
        self.assertTrue(page["has_older"])
        older = self.store.read(ALICE, before=page["messages"][0]["sequence"])
        self.assertEqual(len(older["messages"]), 20)
        self.assertFalse(older["has_older"])
        revision = page["conversation"]["revision"]
        self.assertEqual(self.store.read(ALICE, after=revision)["messages"], [])
        message = older["messages"][1]
        self.store.update_proposal(ALICE, message["id"], "applied")
        changed = self.store.read(ALICE, after=revision)
        self.assertEqual([m["id"] for m in changed["messages"]], [message["id"]])
        self.assertEqual(changed["messages"][0]["proposal_status"], "applied")

    def test_expired_turn_is_failed_without_replaying_the_model(self):
        self.turn(pending=True)
        with self.store.engine.begin() as connection:
            connection.execute(text("UPDATE chat_turns SET heartbeat_at='2000-01-01T00:00:00+00:00'"))
        page = self.store.read(ALICE)
        self.assertIsNone(page["conversation"]["pending_turn_id"])
        self.assertEqual(page["messages"][-1]["status"], "failed")
        self.assertTrue(self.store.heartbeat(ALICE, "turn-first"))
        _, _, status = self.store.start(ALICE, turn_id="turn-first", request_sha256="turn-first", user_message="Design my pipeline")
        self.assertEqual(status, 500)

    def test_concurrent_workers_claim_one_turn(self):
        worker = ConversationStore(self.location)
        def start(store):
            try:
                store.start(ALICE, turn_id="same-turn", request_sha256="same", user_message="one request")
                return "claimed"
            except ConversationError as error:
                return error.code
        try:
            with ThreadPoolExecutor(2) as executor:
                results = list(executor.map(start, [self.store, worker]))
            self.assertCountEqual(results, ["claimed", "turn_pending"])
            self.assertEqual(len(self.store.read(ALICE)["messages"]), 1)
        finally:
            worker.engine.dispose()


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        self.store = ConversationStore(":memory:")
        self.patches = [patch("analytics_api.conversation_store", return_value=self.store),
                        patch("chat_routes.conversation_store", return_value=self.store)]
        for item in self.patches: item.start()
        self.client = analytics_api.app.test_client()

    def tearDown(self):
        for item in self.patches: item.stop()
        self.store.engine.dispose()

    def test_post_is_persisted_once_and_receipt_replayed_without_another_model_call(self):
        payload = {"turn_id": str(uuid.uuid4()), "user_message": "Create my pipeline"}
        async def editor(**kwargs):
            return PipelineEditorTurnResult("Created", {"nodes": [], "edges": []}, {"guardrail_passed": True})
        with patch("analytics_api.llm_config_from_payload", return_value=SimpleNamespace(model="test", provider="test")), \
             patch("analytics_api.log_llm_selection"), patch("analytics_api.run_pipeline_editor_turn", side_effect=editor) as model:
            first = self.client.post("/simple_chat", json=payload)
            second = self.client.post("/simple_chat", json=payload)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json, second.json)
        self.assertEqual(model.call_count, 1)
        page = self.client.get("/api/chat/conversation")
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertEqual([m["role"] for m in page.json["messages"]], ["user", "assistant"])
        self.assertEqual(page.json["conversation"]["id"], first.json["session_id"])
        self.assertEqual(self.client.get("/api/chat/conversation?after=-1").status_code, 400)

    def test_exception_is_persisted_without_private_provider_details(self):
        async def editor(**kwargs):
            raise RuntimeError("provider leaked a private-api-key")
        with patch("analytics_api.llm_config_from_payload", return_value=SimpleNamespace(model="test", provider="test")), \
             patch("analytics_api.log_llm_selection"), patch("analytics_api.run_pipeline_editor_turn", side_effect=editor):
            response = self.client.post("/simple_chat", json={"turn_id": str(uuid.uuid4()), "user_message": "Create"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-api-key", str(response.json))
        page = self.client.get("/api/chat/conversation")
        self.assertEqual(page.json["messages"][-1]["status"], "failed")
        self.assertNotIn("private-api-key", str(page.json))

    def test_cancel_before_registration_stops_without_a_model_call(self):
        turn = str(uuid.uuid4())
        cancelled = self.client.post('/simple_chat/cancel', json={'turn_id': turn, 'session_id': 'another-user-session'})
        self.assertEqual(cancelled.status_code, 202)
        with patch('analytics_api.llm_config_from_payload', return_value=SimpleNamespace(model='test', provider='test')), \
             patch('analytics_api.log_llm_selection'), patch('analytics_api.run_pipeline_editor_turn') as model:
            result = self.client.post('/simple_chat', json={'turn_id': turn, 'user_message': 'Create'})
        self.assertEqual(result.status_code, 409, result.json)
        model.assert_not_called()
        self.assertEqual(self.client.get('/api/chat/conversation').json['messages'][-1]['status'], 'cancelled')

    def test_members_in_the_same_workspace_cannot_read_or_modify_each_others_chat(self):
        cid, _, _ = self.store.start(ALICE, turn_id='private-turn', request_sha256='private', user_message='Alice only')
        response = {'assistant_message': 'Private proposal', 'sync': {'preview_pending': True}}
        self.store.finish(ALICE, 'private-turn', response)
        other = SimpleNamespace(user_id='other-member', workspace_id=ALICE['workspace_id'])
        with patch('conversations.current_principal', return_value=other):
            page = self.client.get('/api/chat/conversation?conversation_id=' + cid)
            self.assertIsNone(page.json['conversation'])
            self.assertEqual(page.json['messages'], [])
            self.assertEqual(self.client.patch('/api/chat/messages/' + response['assistant_message_id'], json={'proposal_status': 'applied'}).status_code, 404)
            self.assertEqual(self.client.delete('/api/chat/conversation', json={'conversation_id': cid}).status_code, 404)
        self.assertEqual(len(self.store.read(ALICE)['messages']), 2)

    def test_workspace_reset_rejects_active_chat_before_clearing_other_stores(self):
        import inlumen_api
        from conversations import chat_scope
        with inlumen_api.app.test_request_context():
            scope = chat_scope()
        self.store.start(scope, turn_id='pending-turn', request_sha256='pending', user_message='Create')
        with patch('inlumen_api.conversation_store', return_value=self.store), patch('inlumen_api._proxy') as graph:
            response = inlumen_api.app.test_client().post('/api/workspace/clear-all', json={})
        self.assertEqual(response.status_code, 409, response.json)
        graph.assert_not_called()
