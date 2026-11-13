import asyncio

import pytest
from fastapi.testclient import TestClient
from inlumen.codegen.llm import generate_json
from inlumen.codegen.schemas import LLMConfig

from app.main import app


class FakeResponse:
    status_code = 200
    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "cost": 0.0042,
            },
            "choices": [
                {"message": {"content": '```json\n{"main_py": "print(\'ok\')"}\n```'}}
            ],
        }


@pytest.mark.parametrize("provider_only", [[], ["cerebras"]])
def test_chat_completions_uses_dedicated_model_and_ephemeral_key(
    monkeypatch, provider_only
) -> None:
    captured: dict = {}
    reported_usage = []

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, body=json)
            return FakeResponse()

    monkeypatch.setattr("inlumen.codegen.llm.httpx.AsyncClient", FakeClient)
    config = LLMConfig(
        provider="openrouter",
        model="openai/gpt-5.2-codex",
        base_url="https://openrouter.ai/api/v1",
        api_key="provider-secret",
        max_output_tokens=8192,
        openrouter_provider_only=provider_only,
    )

    payload = asyncio.run(
        generate_json(
            config,
            system_prompt="system",
            user_prompt="user",
            usage_callback=reported_usage.append,
        )
    )

    assert payload == {"main_py": "print('ok')"}
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["body"]["model"] == "openai/gpt-5.2-codex"
    assert captured["body"]["max_tokens"] == 8192
    if provider_only:
        assert captured["body"]["provider"] == {"only": provider_only}
    else:
        assert "provider" not in captured["body"]
    assert captured["headers"]["Authorization"] == "Bearer provider-secret"
    assert captured["headers"]["HTTP-Referer"] == (
        "https://github.com/DATAPACT/inLUMEN"
    )
    assert captured["headers"]["X-OpenRouter-Title"] == "inLUMEN"
    assert reported_usage[0].model_dump() == {
        "request_count": 1,
        "usage_reported_count": 1,
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "cost_usd": 0.0042,
    }
    assert "provider-secret" not in config.model_dump_json()


def test_node_endpoint_uses_ai_payload_without_generating_dockerfile(
    monkeypatch,
) -> None:
    async def fake_generate(_config, request, _usage_callback=None):
        from inlumen.codegen.generator import fallback_script_payload

        return fallback_script_payload(request)

    monkeypatch.setattr(
        "inlumen.codegen.generator.generate_node_payload", fake_generate
    )
    response = TestClient(app).post(
        "/v1/generate/node-script",
        headers={"X-LLM-API-Key": "provider-secret"},
        json={
            "context": {
                "target_node": {
                    "flow_id": "clean",
                    "label": "Clean data",
                    "type": "task",
                }
            },
            "llm_config": {
                "provider": "openrouter",
                "model": "openai/gpt-5.2-codex",
                "base_url": "https://openrouter.ai/api/v1",
            },
        },
    )

    assert response.status_code == 200
    artifact = response.json()["generated_artifact"]
    filenames = {item["filename"] for item in artifact["files"]}
    assert not any(name.startswith("Dockerfile") for name in filenames)
    assert any(
        "openai/gpt-5.2-codex" in warning
        for warning in artifact["validation_report"]["warnings"]
    )


@pytest.mark.parametrize(
    "model,api_format",
    [
        ("gpt-4o", "chat_completions"),
        ("gpt-5.5", "chat_completions"),
        ("gpt-5.5-2026-04-23", "chat_completions"),
        ("o3", "chat_completions"),
        ("gpt-6.1-sol", "responses"),
        ("gpt-6-astra", "responses"),
        ("gpt-6.1-terra", "responses"),
        ("gpt-6-sol", "responses"),
        ("gpt-6-luna", "responses"),
        ("gpt-5.2-codex", "responses"),
    ],
)
def test_openai_codegen_request_and_usage(monkeypatch, model, api_format):
    import httpx

    captured = []

    async def post(self, url, *, headers, json):
        captured.append((url, json))
        payload = FakeResponse().json()
        if api_format == "responses":
            payload = {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"main_py": "print(1)"}'}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "total_tokens": 150,
                },
            }
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr("httpx.AsyncClient.post", post)
    reported = []
    config = LLMConfig(
        provider="custom",
        model=model,
        base_url="https://api.openai.com/v1",
        api_key="test-secret",
        max_output_tokens=8192,
        top_p=0.9,
    )
    result = asyncio.run(
        generate_json(
            config,
            system_prompt="Return JSON",
            user_prompt="code",
            usage_callback=reported.append,
        )
    )
    assert "main_py" in result
    url, body = captured[0]
    assert url.endswith(
        "/responses" if api_format == "responses" else "/chat/completions"
    )
    field = (
        "max_output_tokens" if api_format == "responses" else "max_completion_tokens"
    )
    assert body[field] == 8192 and "max_tokens" not in body
    assert ("temperature" in body) == (model == "gpt-4o")
    assert ("top_p" in body) == (model == "gpt-4o")
    if api_format == "responses":
        assert body["text"]["format"] == {"type": "json_object"}
    assert reported[0].prompt_tokens == 120
    assert reported[0].completion_tokens == 30
    assert reported[0].cost_usd is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 503])
def test_codegen_preserves_provider_error_without_secret(monkeypatch, status):
    import httpx
    from inlumen.codegen.llm import LLMGenerationError

    async def post(self, url, **kwargs):
        return httpx.Response(
            status,
            request=httpx.Request("POST", url),
            json={
                "error": {
                    "message": "Unsupported max_tokens provider-secret",
                    "code": "unsupported_parameter",
                    "param": "max_tokens",
                }
            },
        )

    monkeypatch.setattr("httpx.AsyncClient.post", post)
    config = LLMConfig(
        provider="custom",
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="provider-secret",
    )
    with pytest.raises(LLMGenerationError) as caught:
        asyncio.run(generate_json(config, system_prompt="JSON", user_prompt="code"))
    assert caught.value.status_code == status
    assert caught.value.code == "unsupported_parameter"
    assert caught.value.param == "max_tokens"
    assert "Unsupported max_tokens" in str(caught.value)
    assert "provider-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "route,target",
    [
        ("node-script", "generate_node_script_bundle"),
        ("pipeline-scripts", "generate_pipeline_script_bundles"),
    ],
)
def test_codegen_endpoint_preserves_provider_status(monkeypatch, route, target):
    from inlumen.codegen.llm import LLMGenerationError

    async def fail(*args, **kwargs):
        raise LLMGenerationError(
            "Model not available to this project.",
            status_code=403,
            code="model_not_available",
            param="model",
        )

    monkeypatch.setattr(f"app.main.{target}", fail)
    response = TestClient(app).post(
        f"/v1/generate/{route}",
        headers={"X-LLM-API-Key": "secret"},
        json={
            "context": {
                "target_node": {"flow_id": "task", "type": "task", "label": "Task"},
                "graph": {"nodes": [], "edges": []},
            },
            "llm_config": {
                "provider": "custom",
                "model": "gpt-5.5",
                "base_url": "https://api.openai.com/v1",
            },
        },
    )
    assert response.status_code == 403, response.text
    assert "Model not available" in response.json()["detail"]["message"]
    assert response.json()["detail"]["code"] == "model_not_available"
    assert response.json()["detail"]["param"] == "model"
