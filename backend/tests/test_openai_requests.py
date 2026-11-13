"""Exercise actual AutoGen/SDK request serialization without network access."""

import asyncio
import json
import sys
from pathlib import Path

import httpx2 as httpx
import pytest
from autogen_core import FunctionCall
from autogen_core.models import (
    AssistantMessage,
    FunctionExecutionResult,
    FunctionExecutionResultMessage,
    SystemMessage,
    UserMessage,
)
from openai import AsyncOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inlumen.llm_requests import provider_error
from llm_config import resolve_llm_config, select_model_client


@pytest.fixture(autouse=True)
def asyncio_context():
    # The application's nest_asyncio patch hides Python 3.14 tasks from sniffio.
    import sniffio

    token = sniffio.current_async_library_cvar.set("asyncio")
    yield
    sniffio.current_async_library_cvar.reset(token)


def client_with_transport(configuration, respond, **kwargs):
    from unittest.mock import patch

    sdk = AsyncOpenAI(
        base_url=configuration.base_url,
        api_key="test-secret",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    with patch(
        "autogen_ext.models.openai._openai_client._openai_client_from_config",
        return_value=sdk,
    ):
        return select_model_client(configuration, **kwargs)


def config(model, **kwargs):
    defaults = {
        "provider": "custom",
        "base_url": "https://api.openai.com/v1",
        "api_key": "test-secret",
        "model": model,
        "max_tokens": 8192,
    }
    defaults.update(kwargs)
    return resolve_llm_config(defaults)


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-5.5", "gpt-5.5-2026-04-23", "o3"])
@pytest.mark.parametrize(
    "host", ["api.openai.com", "eu.api.openai.com", "us.api.openai.com"]
)
def test_chat_serialization(model, host):
    captured = []

    def respond(request):
        assert request.url.host == host
        assert request.url.path == "/v1/chat/completions"
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
            },
        )

    async def run():
        client = client_with_transport(
            config(model, base_url=f"https://{host}/v1", temperature=0.2, top_p=0.9),
            respond,
        )
        try:
            result = await client.create([UserMessage(content="hello", source="user")])
            assert result.content == "hello"
            assert result.usage.completion_tokens == 2
        finally:
            await client.close()

    asyncio.run(run())
    body = captured[0]
    assert body["max_completion_tokens"] == 8192
    assert "max_tokens" not in body
    assert ("temperature" in body) == (model == "gpt-4o")
    assert ("top_p" in body) == (model == "gpt-4o")


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6.1-terra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.2-codex",
    ],
)
@pytest.mark.parametrize(
    "host", ["api.openai.com", "eu.api.openai.com", "us.api.openai.com"]
)
def test_responses_tool_roundtrip(model, host):
    captured = []

    def respond(request):
        assert request.url.host == host
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        captured.append(body)
        output = (
            [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": "opaque",
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "inspect",
                    "arguments": "{}",
                    "status": "completed",
                },
            ]
            if len(captured) == 1
            else [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "done", "annotations": []}
                    ],
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1,
                "model": model,
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        )

    async def run():
        client = client_with_transport(
            config(
                model,
                base_url=f"https://{host}/v1",
                reasoning_effort="low",
                temperature=0.1,
                top_p=0.9,
            ),
            respond,
            parallel_tool_calls=False,
        )
        messages = [
            SystemMessage(content="help"),
            UserMessage(content="inspect", source="user"),
        ]
        tools = [
            {
                "name": "inspect",
                "description": "Inspect graph",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        try:
            result = await client.create(messages, tools=tools)
            assert result.content == [
                FunctionCall(id="call_1", name="inspect", arguments="{}")
            ]
            messages.extend(
                [
                    AssistantMessage(content=result.content, source="assistant"),
                    FunctionExecutionResultMessage(
                        content=[
                            FunctionExecutionResult(
                                call_id="call_1", name="inspect", content="ok"
                            )
                        ]
                    ),
                ]
            )
            result = await client.create(messages, tools=tools)
            assert result.content == "done"
            assert client.total_usage().prompt_tokens == 10
        finally:
            await client.close()

    asyncio.run(run())
    first, second = captured
    assert first["max_output_tokens"] == 8192
    assert first["reasoning"] == {"effort": "low"}
    assert "temperature" not in first and "top_p" not in first
    assert first["parallel_tool_calls"] is False
    assert first["tools"][0]["name"] == "inspect"
    assert first["tools"][0]["strict"] is False
    assert first["store"] is False
    assert second["input"][2]["encrypted_content"] == "opaque"
    assert second["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "ok",
    }


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (
            {"model": "gpt-6.1-sol", "api_format": "chat_completions"},
            "requires the Responses API",
        ),
        ({"model": "gpt-6.1-sol", "reasoning_effort": "none"}, "does not support"),
        ({"model": "gpt-6-luna", "reasoning_effort": "minimal"}, "does not support"),
        ({"model": "gpt-4o", "reasoning_effort": "low"}, "does not support"),
        ({"model": "gpt-5.5", "api_format": "invalid"}, "api_format"),
    ],
)
def test_invalid_config_fails_before_provider_request(kwargs, match):
    with pytest.raises(ValueError, match=match):
        config(**kwargs)


def test_error_extraction_redacts_credentials():
    from openai import BadRequestError

    original = BadRequestError(
        "bad",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://api.openai.com")
        ),
        body={
            "message": "Unsupported max_tokens test-secret Bearer abc sk-123456",
            "code": "unsupported_parameter",
            "param": "max_tokens",
        },
    )
    wrapper = RuntimeError("agent failed")
    wrapper.__cause__ = original
    error = provider_error(wrapper, secrets=("test-secret",))
    assert error.status_code == 400
    assert error.param == "max_tokens"
    assert error.code == "unsupported_parameter"
    assert "Unsupported max_tokens" in str(error)
    assert all(
        secret not in str(error) for secret in ("test-secret", "abc", "sk-123456")
    )
    assert provider_error(RuntimeError("private internal error")) is None


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-6.1-sol"])
def test_provider_status_survives_real_agent_runtime(model):
    from autogen_agentchat.agents import AssistantAgent
    from inlumen.design.team import ProviderAwareGroupChat
    from inlumen.llm_requests import ProviderRequestError

    def respond(request):
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "Quota exceeded test-secret",
                    "code": "insufficient_quota",
                }
            },
        )

    async def run():
        client = client_with_transport(config(model), respond)
        client._client.max_retries = 0
        team = ProviderAwareGroupChat(
            [AssistantAgent("editor", model_client=client)], max_turns=1
        )
        try:
            with pytest.raises(ProviderRequestError) as caught:
                await team.run(task="Hello")
            assert caught.value.status_code == 429
            assert caught.value.code == "insufficient_quota"
            assert "Quota exceeded" in str(caught.value)
            assert "test-secret" not in str(caught.value)
        finally:
            await client.close()

    asyncio.run(run())


def test_responses_structured_output_and_stream_interface():
    from pydantic import BaseModel

    class Answer(BaseModel):
        answer: str

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"answer":"ok"}',
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        )

    async def run():
        client = client_with_transport(config("gpt-6.1-sol"), respond)
        try:
            results = [
                r
                async for r in client.create_stream(
                    [UserMessage(content="Answer in JSON", source="user")],
                    json_output=Answer,
                )
            ]
            assert len(results) == 1
            assert results[0].content == '{"answer":"ok"}'
        finally:
            await client.close()

    asyncio.run(run())
    fmt = captured[0]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert fmt["schema"]["additionalProperties"] is False


def test_responses_request_honors_cancellation():
    from autogen_core import CancellationToken

    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.Event().wait()

    async def run():
        client = client_with_transport(config("gpt-6.1-sol"), respond)
        token = CancellationToken()
        try:
            task = asyncio.create_task(
                client.create(
                    [UserMessage(content="hello", source="user")],
                    cancellation_token=token,
                )
            )
            started_task = asyncio.create_task(started.wait())
            done, _ = await asyncio.wait(
                {task, started_task}, timeout=2, return_when=asyncio.FIRST_COMPLETED
            )
            assert started_task in done, "Provider request did not start"
            token.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("provider_only", [[], ["openai"]])
def test_openrouter_responses_preserves_provider_routing_and_timeout(provider_only):
    captured = []

    def respond(request):
        assert request.url.host == "openrouter.ai"
        assert request.url.path == "/api/v1/responses"
        assert request.extensions["timeout"]["read"] == 10.0
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_routing",
                "object": "response",
                "created_at": 1,
                "model": "openai/gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_routing",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "done", "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        )

    async def run():
        client = client_with_transport(
            config(
                "openai/gpt-6.1-sol",
                provider="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_format="responses",
                openrouter_provider_only=provider_only,
            ),
            respond,
        )
        try:
            result = await client.create(
                [UserMessage(content="hello", source="user")],
                extra_create_args={"timeout": 10.0},
            )
            assert result.content == "done"
        finally:
            await client.close()

    asyncio.run(run())
    body = captured[0]
    assert body["model"] == "openai/gpt-6.1-sol"
    assert body["max_output_tokens"] == 8192
    if provider_only:
        assert body["provider"] == {"only": provider_only}
    else:
        assert "provider" not in body
