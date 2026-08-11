"""Thread-safe cancellation for in-flight pipeline editor turns."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Coroutine


_STALE_TURN_SECONDS = 60 * 60


@dataclass
class _TurnControl:
    requested_at: float | None = None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[Any] | None = None


class PipelineTurnRegistry:
    """Coordinates cancellation requests across Flask request threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[str, _TurnControl] = {}

    def _prune_locked(self, now: float) -> None:
        stale_ids = [
            turn_id
            for turn_id, control in self._turns.items()
            if control.task is None
            and control.requested_at is not None
            and now - control.requested_at > _STALE_TURN_SECONDS
        ]
        for turn_id in stale_ids:
            self._turns.pop(turn_id, None)

    def register(
        self,
        turn_id: str,
        loop: asyncio.AbstractEventLoop,
        task: asyncio.Task[Any],
    ) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            control = self._turns.setdefault(turn_id, _TurnControl())
            control.loop = loop
            control.task = task
            cancellation_requested = control.requested_at is not None
        if cancellation_requested:
            task.cancel()

    def request_cancel(self, turn_id: str) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            control = self._turns.setdefault(turn_id, _TurnControl())
            control.requested_at = now
            loop = control.loop
            task = control.task
            active = bool(loop and task and not task.done())
        if active and loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)
        return {
            "turn_id": turn_id,
            "status": "cancelling" if active else "cancel_queued",
            "active": active,
        }

    def finish(self, turn_id: str) -> None:
        with self._lock:
            self._turns.pop(turn_id, None)


PIPELINE_TURN_REGISTRY = PipelineTurnRegistry()


def request_pipeline_turn_cancel(turn_id: str) -> dict[str, Any]:
    return PIPELINE_TURN_REGISTRY.request_cancel(turn_id)


def run_cancellable_pipeline_turn(
    turn_id: str,
    coroutine: Coroutine[Any, Any, Any],
) -> Any:
    """Run one turn on a dedicated loop that another request thread can cancel."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(coroutine)
    PIPELINE_TURN_REGISTRY.register(turn_id, loop, task)
    try:
        return loop.run_until_complete(task)
    finally:
        PIPELINE_TURN_REGISTRY.finish(turn_id)
        asyncio.set_event_loop(None)
        loop.close()
