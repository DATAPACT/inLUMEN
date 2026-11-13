"""Run the design agent on an in-memory graph: the entry point for other programs.

    from inlumen.design.run import design, openai_model_client

    result = await design(
        "Read invoices, extract totals, save a CSV report",
        model=openai_model_client("openai/gpt-4.1", base_url="https://openrouter.ai/api/v1", api_key=key),
    )
    result.project        # inLUMEN Project JSON, opens with the canvas Import button
    result.requests       # one record per model request (tokens, latency, outcome)
"""
from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from autogen_core.models import ChatCompletionClient, CreateResult, ModelInfo

from inlumen.contracts.events import EventSink, model_request_event  # noqa: F401  (EventSink re-exported)
from inlumen.contracts.plugins import use_plugin_source
from inlumen.contracts.project_ir import project_document

from .catalog import Catalog, package_version
from .memory_store import InMemoryGraphStore
from .team import build_design_team
from .turn import run_design_turn



class RecordingModelClient(ChatCompletionClient):
    """Wraps a model client and reports every request to an event sink.

    Each event is :func:`inlumen.contracts.events.model_request_event`'s
    dict; the cost is None (autogen clients do not report one).
    """

    def __init__(self, inner: ChatCompletionClient, sink: EventSink, *, purpose: str = "design"):
        self._inner = inner
        self._sink = sink
        self.purpose = purpose

    @property
    def model_name(self) -> str:
        create_args = getattr(self._inner, "_create_args", None)
        return str(create_args.get("model") or "") if isinstance(create_args, dict) else ""

    def _emit(self, started: float, result: CreateResult | None, error: BaseException | str | None) -> None:
        usage = result.usage if result is not None else None
        if isinstance(error, BaseException):
            error = f"{type(error).__name__}: {error}"
        self._sink(model_request_event(
            purpose=self.purpose,
            model=self.model_name,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            latency_s=time.monotonic() - started,
            finish_reason=getattr(result, "finish_reason", None),
            error=error,
        ))

    async def create(self, messages: Sequence[Any], **kwargs: Any) -> CreateResult:
        started = time.monotonic()
        try:
            result = await self._inner.create(messages, **kwargs)
        except BaseException as exc:
            self._emit(started, None, exc)
            raise
        self._emit(started, result, None)
        return result

    async def create_stream(self, messages: Sequence[Any], **kwargs: Any) -> AsyncGenerator[str | CreateResult, None]:
        started = time.monotonic()
        result: CreateResult | None = None
        try:
            async for chunk in self._inner.create_stream(messages, **kwargs):
                if isinstance(chunk, CreateResult):
                    result = chunk
                yield chunk
        except GeneratorExit:
            # The consumer stopped reading: a success if the final result arrived.
            self._emit(started, result, None if result is not None else "stream closed before the result")
            raise
        except BaseException as exc:
            self._emit(started, None, exc)
            raise
        self._emit(started, result, None)

    async def close(self) -> None:
        await self._inner.close()

    def actual_usage(self):
        return self._inner.actual_usage()

    def total_usage(self):
        return self._inner.total_usage()

    def count_tokens(self, messages: Sequence[Any], **kwargs: Any) -> int:
        return self._inner.count_tokens(messages, **kwargs)

    def remaining_tokens(self, messages: Sequence[Any], **kwargs: Any) -> int:
        return self._inner.remaining_tokens(messages, **kwargs)

    @property
    def capabilities(self):  # deprecated in autogen, still abstract
        return self._inner.capabilities

    @property
    def model_info(self) -> ModelInfo:
        return self._inner.model_info


def openai_model_client(
    model: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model_info: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ChatCompletionClient:
    """An OpenAI-compatible client (OpenAI, OpenRouter, Ollama, vLLM, …) for the agent.

    ``model_info`` defaults to a tool-calling model of unknown family. Parallel
    tool calls are off: the agent's graph edits must run one at a time.
    """
    from inlumen.llm_autogen import OpenAIResponsesClient, ProviderAwareChatClient as OpenAIChatCompletionClient
    from inlumen.llm_requests import request_policy

    info = {
        "vision": False,
        "function_calling": True,
        "json_output": True,
        "family": "unknown",
        "structured_output": False,
        **(model_info or {}),
    }
    policy = request_policy(
        model,
        base_url or "https://api.openai.com/v1",
        api_format=kwargs.pop("api_format", "auto"),
        reasoning_effort=kwargs.pop("reasoning_effort", None),
        tools=True,
    )
    limit = kwargs.pop("max_completion_tokens", kwargs.pop("max_tokens", None))
    chat_policy = replace(policy, api_format="chat_completions")
    kwargs.update(
        chat_policy.parameters(
            limit,
            temperature=kwargs.pop("temperature", None),
            top_p=kwargs.pop("top_p", None),
        )
    )
    client_class = (
        OpenAIResponsesClient
        if policy.api_format == "responses"
        else OpenAIChatCompletionClient
    )
    if policy.api_format == "responses":
        kwargs["request_policy"] = policy
    return client_class(
        model=model,
        base_url=base_url,
        api_key=api_key or "not-needed",
        model_info=info,
        parallel_tool_calls=False,
        **kwargs,
    )


@dataclass
class DesignResult:
    """The outcome of one design request."""

    message: str  # the agent's reply
    graph: dict  # canvas graph (nodes, edges, pipeline)
    project: dict  # inLUMEN Project JSON of ``graph``
    sync: dict  # guardrail verdict, see inlumen.design.turn.DesignTurn
    accepted: bool  # the result passed validation (else ``graph`` is the starting graph)
    repaired: bool  # a repair round ran
    requests: list[dict[str, Any]] = field(default_factory=list)
    package_version: str = ""
    catalog_fingerprint: str = ""

    @property
    def usage(self) -> dict[str, int]:
        return {
            "requests": len(self.requests),
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in self.requests),
            "completion_tokens": sum(r.get("completion_tokens") or 0 for r in self.requests),
        }


async def design(
    request: str,
    *,
    model: ChatCompletionClient,
    catalog: Catalog | None = None,
    graph: dict | None = None,
    name: str = "",
    events: EventSink | None = None,
    max_tool_iterations: int = 30,
) -> DesignResult:
    """Design (or edit) a pipeline for ``request`` on an in-memory graph.

    ``graph`` is the starting canvas graph (default: empty). When the agent's
    result fails validation even after one repair round, it is rejected: the
    store is rolled back and ``accepted`` is False, as in inLUMEN.
    """
    catalog = catalog or Catalog.default()
    requests: list[dict[str, Any]] = []

    def record(event: dict[str, Any]) -> None:
        requests.append(event)
        if events is not None:
            events(event)

    store = InMemoryGraphStore(deepcopy(graph) if graph else None, reusable_pipelines=catalog.reusable_pipelines)
    before = store.graph()
    client = RecordingModelClient(model, record)
    with use_plugin_source(catalog.plugin_source()):
        team = build_design_team(
            client,
            store,
            node_definitions=catalog.node_definitions,
            max_tool_iterations=max_tool_iterations,
        )
        turn = await run_design_turn(
            user_message=request,
            team=team,
            graph=store,
            canvas_graph=before if graph else None,
            agent_graph=before,
            visible_before_graph=before,
        )
        accepted = bool(turn.sync.get("guardrail_passed"))
        if not accepted:
            # Whatever the reason (invalid, or unverifiable), a rejected design
            # is not kept: the result is the starting graph.
            await store.restore_graph(before)
        final_graph = turn.graph if accepted and isinstance(turn.graph, dict) else store.graph()
        project = project_document(final_graph, name=name, description=request)
    return DesignResult(
        message=turn.assistant_message,
        graph=final_graph,
        project=project,
        sync=turn.sync,
        accepted=accepted,
        repaired=turn.repaired,
        requests=requests,
        package_version=package_version(),
        catalog_fingerprint=catalog.fingerprint(),
    )
