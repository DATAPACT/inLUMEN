import asyncio

from fastapi.testclient import TestClient

from app.llm import generate_json
from app.main import app
from app.schemas import LLMConfig


class FakeResponse:
    status_code = 200
    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": "```json\n{\"main_py\": \"print('ok')\"}\n```"
                    }
                }
            ]
        }


def test_chat_completions_uses_dedicated_model_and_ephemeral_key(monkeypatch) -> None:
    captured: dict = {}

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

    monkeypatch.setattr("app.llm.httpx.AsyncClient", FakeClient)
    config = LLMConfig(
        provider="openrouter",
        model="openai/gpt-5.2-codex",
        base_url="https://openrouter.ai/api/v1",
        api_key="provider-secret",
    )

    payload = asyncio.run(
        generate_json(config, system_prompt="system", user_prompt="user")
    )

    assert payload == {"main_py": "print('ok')"}
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["body"]["model"] == "openai/gpt-5.2-codex"
    assert captured["headers"]["Authorization"] == "Bearer provider-secret"
    assert "provider-secret" not in config.model_dump_json()


def test_node_endpoint_uses_ai_payload_without_generating_dockerfile(monkeypatch) -> None:
    async def fake_generate(_config, request):
        from app.generator import fallback_script_payload

        return fallback_script_payload(request)

    monkeypatch.setattr("app.generator.generate_node_payload", fake_generate)
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
