"""How the code generator reaches a model, and who hears about each request.

Every model call goes through :func:`inlumen.codegen.llm.generate_json`. By
default it posts to the OpenAI-compatible endpoint in the request's
``LLMConfig`` (:class:`HttpModelClient`). A library caller can install any
:class:`ModelClient` for one run with :func:`use_model`, and receive one event
per request with :func:`use_events`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Receives one dict per model request (same shape as inlumen.design.run).
EventSink = Callable[[dict[str, Any]], None]


@dataclass
class ModelReply:
    """One chat completion: the message content and the provider's usage block.

    ``usage`` uses the chat-completions keys (``prompt_tokens``,
    ``completion_tokens``, ``total_tokens`` and, when known, ``cost`` in USD).
    """

    content: Any
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@runtime_checkable
class ModelClient(Protocol):
    """Answers one system + user prompt; the generator parses the JSON reply."""

    name: str

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_output: bool,
        max_tokens: int,
    ) -> ModelReply: ...


CURRENT_MODEL: ContextVar[ModelClient | None] = ContextVar("codegen_model", default=None)
CURRENT_EVENTS: ContextVar[EventSink | None] = ContextVar("codegen_events", default=None)


@contextmanager
def use_model(client: ModelClient | None) -> Iterator[None]:
    """Send this context's model requests to ``client`` instead of the LLMConfig endpoint."""
    token = CURRENT_MODEL.set(client)
    try:
        yield
    finally:
        CURRENT_MODEL.reset(token)


@contextmanager
def use_events(sink: EventSink | None) -> Iterator[None]:
    """Report this context's model requests to ``sink``."""
    token = CURRENT_EVENTS.set(sink)
    try:
        yield
    finally:
        CURRENT_EVENTS.reset(token)
