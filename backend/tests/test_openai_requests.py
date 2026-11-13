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
        ({"model": "gpt-5.5-pro", "reasoning_effort": "none"}, "does not support"),
        (
            {"model": "gpt-5.5-pro-2026-04-23", "reasoning_effort": "low"},
            "does not support",
        ),
        ({"model": "gpt-5.2-codex", "reasoning_effort": "none"}, "does not support"),
        ({"model": "gpt-5.2-codex", "reasoning_effort": "minimal"}, "does not support"),
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


@pytest.mark.parametrize(
    "response_status,call_status",
    [
        ("incomplete", "incomplete"),
        ("incomplete", "completed"),
        ("completed", "incomplete"),
        ("completed", "in_progress"),
    ],
)
def test_unfinished_responses_never_execute_agent_tools(response_status, call_status):
    from autogen_agentchat.agents import AssistantAgent
    from inlumen.design.team import ProviderAwareGroupChat
    from inlumen.llm_requests import ProviderRequestError

    executions = []
    requests = []

    def change(value: int) -> str:
        """Change a pipeline value."""
        executions.append(value)
        return "changed"

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_unfinished",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6.1-sol",
                "status": response_status,
                "incomplete_details": {"reason": "max_output_tokens"}
                if response_status == "incomplete"
                else None,
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_test",
                        "call_id": "call_test",
                        "name": "change",
                        "arguments": '{"value":1}',
                        "status": call_status,
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
            },
        )

    async def run():
        client = client_with_transport(
            config("gpt-6.1-sol"), respond, parallel_tool_calls=False
        )
        agent = AssistantAgent(
            "editor", model_client=client, tools=[change], reflect_on_tool_use=True
        )
        team = ProviderAwareGroupChat([agent], max_turns=1)
        try:
            with pytest.raises(ProviderRequestError) as caught:
                await team.run(task="Change a pipeline value")
            assert caught.value.status_code == 502
            assert caught.value.code == "incomplete_response"
            expected_reason = (
                "max_output_tokens"
                if response_status == "incomplete"
                else "unfinished_output"
            )
            assert expected_reason in str(caught.value)
            assert executions == []
            assert client._response_replay == {}
            assert len(requests) == 1
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort", [None, "none"])
def test_chat_overrides_are_normalized_on_the_wire(stream, effort):
    from inlumen.llm_requests import ProviderRequestError

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "captured"}})

    async def run():
        client = client_with_transport(config("gpt-5.5", top_p=0.7), respond)
        overrides = {"max_tokens": 1024, "temperature": 0.2}
        if effort is not None:
            overrides["reasoning_effort"] = effort
        messages = [UserMessage(content="test", source="user")]
        try:
            with pytest.raises(ProviderRequestError, match="captured"):
                if stream:
                    async for _ in client.create_stream(
                        messages, extra_create_args=overrides
                    ):
                        pass
                else:
                    await client.create(messages, extra_create_args=overrides)
        finally:
            await client.close()

    asyncio.run(run())
    (body,) = captured
    assert body["max_completion_tokens"] == 1024
    assert "max_tokens" not in body
    assert body.get("temperature") == (0.2 if effort == "none" else None)
    assert body.get("top_p") == (0.7 if effort == "none" else None)


def test_design_factory_serializes_native_limit_and_redacts_failed_response():
    from inlumen.design.run import openai_model_client
    from inlumen.llm_requests import ProviderRequestError

    def respond(request):
        assert json.loads(request.content)["max_output_tokens"] == 1024
        return httpx.Response(
            200,
            json={
                "id": "r",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6.1-sol",
                "status": "failed",
                "output": [],
                "error": dict.fromkeys(("code", "param", "message"), "test-credential"),
            },
        )

    async def run():
        client = openai_model_client(
            "gpt-6.1-sol",
            api_key="test-credential",
            max_output_tokens=1024,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        try:
            with pytest.raises(ProviderRequestError) as caught:
                await client.create([UserMessage(content="test", source="user")])
            assert (
                str(caught.value)
                == caught.value.code
                == caught.value.param
                == "[redacted]"
            )
        finally:
            await client.close()

    asyncio.run(run())


def test_saved_team_replays_encrypted_tool_turn_with_fresh_client():
    from autogen_agentchat.agents import AssistantAgent
    from inlumen.design.team import ProviderAwareGroupChat

    requests = []

    def inspect_graph() -> str:
        return "ok"

    def respond(request):
        requests.append(json.loads(request.content))
        output = (
            [
                {
                    "type": "reasoning",
                    "id": "rs",
                    "summary": [],
                    "encrypted_content": "opaque",
                },
                {
                    "type": "function_call",
                    "id": "fc",
                    "call_id": "call",
                    "name": "inspect_graph",
                    "arguments": "{}",
                    "status": "completed",
                },
            ]
            if len(requests) == 1
            else [
                {
                    "type": "message",
                    "id": "msg",
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
                "id": "r",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": output,
            },
        )

    async def run():
        saved = None
        for task in ("Inspect", "Continue"):
            client = client_with_transport(config("gpt-6.1-sol"), respond)
            agent = AssistantAgent(
                "editor",
                model_client=client,
                tools=[inspect_graph],
                reflect_on_tool_use=True,
            )
            team = ProviderAwareGroupChat([agent], max_turns=1, model_client=client)
            try:
                if saved is not None:
                    await team.load_state(saved)
                await team.run(task=task)
                saved = json.loads(json.dumps(await team.save_state()))
            finally:
                await client.close()

    asyncio.run(run())
    assert len(requests) == 3
    items = requests[-1]["input"]
    replay = next(
        i for i, item in enumerate(items) if item.get("encrypted_content") == "opaque"
    )
    assert items[replay + 1]["call_id"] == items[replay + 2]["call_id"] == "call"
