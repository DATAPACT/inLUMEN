"""Responses adapter for the AutoGen 0.7 chat-client interface.

Reuse AutoGen's message/tool conversion and accounting interface. Requests are
stateless, with reasoning output replayed during tool loops; provider storage is
disabled. Streaming callers receive one complete result.
"""

from __future__ import annotations

import asyncio
from functools import wraps

from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.openai import OpenAIChatCompletionClient
from openai.lib._pydantic import to_strict_json_schema

from .llm_requests import (
    provider_error,
    remember_provider_error,
    request_policy,
    responses_request,
    responses_result,
)


def _preserve_provider_error(method):
    @wraps(method)
    async def call(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except Exception as exc:
            error = provider_error(exc, secrets=(self._client.api_key,))
            if error is not None:
                remember_provider_error(error)
                raise error from None
            raise

    return call


class ProviderAwareChatClient(OpenAIChatCompletionClient):
    @_preserve_provider_error
    async def create(self, *args, **kwargs):
        return await super().create(*args, **kwargs)

    async def create_stream(self, *args, **kwargs):
        try:
            async for chunk in super().create_stream(*args, **kwargs):
                yield chunk
        except Exception as exc:
            error = provider_error(exc, secrets=(self._client.api_key,))
            if error is not None:
                remember_provider_error(error)
                raise error from None
            raise


class OpenAIResponsesClient(ProviderAwareChatClient):
    def __init__(self, *, request_policy, **kwargs):
        super().__init__(**kwargs)
        self._request_policy = request_policy
        self._response_replay = {}

    @_preserve_provider_error
    async def create(
        self,
        messages,
        *,
        tools=(),
        tool_choice="auto",
        json_output=None,
        extra_create_args=None,
        cancellation_token=None,
    ):
        params = self._process_create_args(
            messages, tools, tool_choice, json_output, extra_create_args or {}
        )
        args = dict(params.create_args)
        fmt = args.pop("response_format", None)
        if params.response_format is not None:
            fmt = {
                "type": "json_schema",
                "json_schema": {
                    "name": params.response_format.__name__,
                    "strict": True,
                    "schema": to_strict_json_schema(params.response_format),
                },
            }
        request = responses_request(
            params.messages,
            tools=params.tools,
            response_format=fmt,
            replay=self._response_replay,
        )
        limit = args.pop("max_completion_tokens", args.pop("max_tokens", None))
        effort = args.pop("reasoning_effort", self._request_policy.effort)
        policy = request_policy(
            args["model"],
            str(self._client.base_url),
            api_format="responses",
            reasoning_effort=effort,
            tools=bool(tools),
        )
        request.update(
            policy.parameters(
                limit,
                temperature=args.pop("temperature", None),
                top_p=args.pop("top_p", None),
            )
        )
        for key in ("model", "parallel_tool_calls", "seed", "user", "service_tier"):
            if key in args:
                # Responses has no seed parameter.
                if key == "seed":
                    raise ValueError("seed is not supported by the Responses API.")
                request[key] = args.pop(key)
        choice = args.pop("tool_choice", None)
        if choice is not None and params.tools:
            request["tool_choice"] = (
                {"type": "function", "name": choice["function"]["name"]}
                if isinstance(choice, dict)
                else choice
            )
        if args:
            raise ValueError(
                f"Unsupported Responses API parameters: {', '.join(sorted(args))}"
            )
        future = asyncio.ensure_future(self._client.responses.create(**request))
        if cancellation_token is not None:
            cancellation_token.link_future(future)
        raw = (await future).model_dump(exclude_none=True)
        text, calls, counts, finish = responses_result(raw)
        if calls:
            self._response_replay[calls[0]["call_id"]] = raw["output"]
        usage = RequestUsage(
            prompt_tokens=counts["prompt_tokens"],
            completion_tokens=counts["completion_tokens"],
        )
        self._total_usage = RequestUsage(
            prompt_tokens=self._total_usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self._total_usage.completion_tokens
            + usage.completion_tokens,
        )
        self._actual_usage = self._total_usage
        return CreateResult(
            content=[
                FunctionCall(id=c["call_id"], name=c["name"], arguments=c["arguments"])
                for c in calls
            ]
            if calls
            else text,
            thought=text if calls and text else None,
            finish_reason=finish,
            usage=usage,
            cached=False,
        )

    async def create_stream(self, messages, **kwargs):
        yield await self.create(messages, **kwargs)
