import asyncio
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_agent.cancellation import (  # noqa: E402
    request_pipeline_turn_cancel,
    run_cancellable_pipeline_turn,
)
from graph_client import run_neo4j_query  # noqa: E402


class PipelineAgentCancellationTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
