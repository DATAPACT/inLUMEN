import asyncio
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_agent.cancellation import (  # noqa: E402
    cancel_pipeline_turn_and_wait,
    request_pipeline_turn_cancel,
    run_cancellable_pipeline_turn,
)
from analytics_api import app  # noqa: E402
from graph_client import run_neo4j_query  # noqa: E402


class PipelineAgentCancellationTest(unittest.TestCase):
    def test_cancel_endpoint_acknowledges_immediately_and_clears_session(self):
        turn_id = f"turn-{uuid.uuid4()}"
        with (
            patch(
                "analytics_api.request_pipeline_turn_cancel",
                return_value={
                    "turn_id": turn_id,
                    "status": "cancelling",
                    "active": True,
                },
            ) as request_cancel,
            patch("analytics_api.clear_state_from_disk") as clear_session,
            app.test_client() as client,
        ):
            response = client.post(
                "/simple_chat/cancel",
                json={"turn_id": turn_id, "session_id": "session-123"},
            )

        self.assertEqual(202, response.status_code)
        payload = response.get_json()
        self.assertEqual("cancelling", payload["status"])
        self.assertFalse(payload["completed"])
        self.assertTrue(payload["session_cleared"])
        request_cancel.assert_called_once_with(turn_id)
        clear_session.assert_called_once_with("session-123")

    def test_waits_for_started_graph_mutation_before_propagating_cancellation(self):
        started = threading.Event()
        release = threading.Event()
        response = MagicMock()
        response.text = "ok"

        def blocking_graph_request(*_args, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return response

        async def exercise():
            with patch("graph_client.dispatch_graph_request", side_effect=blocking_graph_request):
                task = asyncio.create_task(run_neo4j_query("CREATE ()", "create_step"))
                started_ok = await asyncio.to_thread(started.wait, 2)
                self.assertTrue(started_ok)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(exercise())

    def test_cancels_an_active_turn_from_another_thread(self):
        turn_id = f"turn-{uuid.uuid4()}"
        started = threading.Event()
        outcome: list[BaseException] = []

        async def wait_forever():
            started.set()
            await asyncio.Event().wait()

        def run_turn():
            try:
                run_cancellable_pipeline_turn(turn_id, wait_forever())
            except BaseException as exc:  # CancelledError inherits BaseException.
                outcome.append(exc)

        thread = threading.Thread(target=run_turn)
        thread.start()
        self.assertTrue(started.wait(timeout=2))

        result = request_pipeline_turn_cancel(turn_id)
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual("cancelling", result["status"])
        self.assertTrue(result["active"])
        self.assertEqual(1, len(outcome))
        self.assertIsInstance(outcome[0], asyncio.CancelledError)

    def test_honors_stop_clicked_before_the_turn_registers(self):
        turn_id = f"turn-{uuid.uuid4()}"
        entered = False

        async def should_not_start():
            nonlocal entered
            entered = True

        queued = request_pipeline_turn_cancel(turn_id)
        with self.assertRaises(asyncio.CancelledError):
            run_cancellable_pipeline_turn(turn_id, should_not_start())

        self.assertEqual("cancel_queued", queued["status"])
        self.assertFalse(queued["active"])
        self.assertFalse(entered)

    def test_synchronous_cancel_waits_for_turn_cleanup(self):
        turn_id = f"turn-{uuid.uuid4()}"
        started = threading.Event()
        cleanup_finished = threading.Event()

        async def run_until_cancelled():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
                cleanup_finished.set()
                error = RuntimeError("cancelled after cleanup")
                error.rollback_applied = True
                raise error

        outcome: list[BaseException] = []

        def run_turn():
            try:
                run_cancellable_pipeline_turn(turn_id, run_until_cancelled())
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=run_turn)
        thread.start()
        self.assertTrue(started.wait(timeout=2))

        result = cancel_pipeline_turn_and_wait(turn_id, timeout=2)
        thread.join(timeout=2)

        self.assertTrue(cleanup_finished.is_set())
        self.assertFalse(thread.is_alive())
        self.assertEqual("cancelled", result["status"])
        self.assertTrue(result["completed"])
        self.assertTrue(result["rollback_applied"])
        self.assertEqual(1, len(outcome))


if __name__ == "__main__":
    unittest.main()
