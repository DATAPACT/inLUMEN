"""Compatibility rules shared by chat and codegen, without provider calls."""

import pytest
from inlumen.llm_requests import endpoint_url, request_policy, responses_request


@pytest.mark.parametrize(
    "provider,url,model",
    [
        ("openrouter", "https://openrouter.ai/api/v1", "openai/gpt-5.5"),
        ("ollama_cloud", "https://ollama.com/v1", "gpt-oss:120b"),
        ("custom", "https://llm.example/v1", "gpt-5.5"),
    ],
)
def test_compatible_provider_defaults_are_preserved(provider, url, model):
    policy = request_policy(model, url, provider=provider, tools=True)
    assert policy.api_format == "chat_completions"
    assert policy.parameters(8192, temperature=0.1, top_p=0.9) == {
        "max_tokens": 8192,
        "temperature": 0.1,
        "top_p": 0.9,
    }


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh"])
def test_gpt55_effort_and_sampling(effort):
    policy = request_policy(
        "gpt-5.5", "https://api.openai.com/v1", reasoning_effort=effort
    )
    args = policy.parameters(1024, temperature=0.2, top_p=0.8)
    assert args["reasoning_effort"] == effort
    assert ("temperature" in args) == (effort == "none")
    assert ("top_p" in args) == (effort == "none")
    assert args["max_completion_tokens"] == 1024


@pytest.mark.parametrize(
    "model,effort",
    [
        ("gpt-5.5", "minimal"),
        ("gpt-5.5", "max"),
        ("gpt-5", "none"),
        ("gpt-5", "xhigh"),
        ("o3", "none"),
        ("gpt-6.1-sol", "none"),
        ("gpt-6-astra", "none"),
        ("gpt-6-luna", "minimal"),
    ],
)
def test_invalid_effort_is_rejected(model, effort):
    with pytest.raises(ValueError, match="does not support"):
        request_policy(model, "https://api.openai.com/v1", reasoning_effort=effort)


def test_explicit_responses_and_chat_overrides():
    policy = request_policy(
        "gpt-5.5",
        "https://api.openai.com/v1",
        api_format="responses",
        reasoning_effort="low",
    )
    assert policy.parameters(1024) == {
        "max_output_tokens": 1024,
        "reasoning": {"effort": "low"},
    }
    policy = request_policy(
        "gpt-6-sol",
        "https://api.openai.com/v1",
        api_format="chat_completions",
        reasoning_effort="none",
        tools=True,
    )
    assert policy.api_format == "chat_completions"
    with pytest.raises(ValueError, match="requires the Responses API"):
        request_policy(
            "gpt-6.1-terra",
            "https://api.openai.com/v1",
            api_format="chat_completions",
            reasoning_effort="none",
            tools=True,
        )


@pytest.mark.parametrize(
    "base",
    [
        "https://api.openai.com",
        "https://api.openai.com/v1/",
        "https://api.openai.com/v1/chat/completions",
        "https://api.openai.com/v1/responses",
    ],
)
def test_endpoint_url(base):
    assert endpoint_url(base, "responses") == "https://api.openai.com/v1/responses"
    assert (
        endpoint_url(base, "chat_completions")
        == "https://api.openai.com/v1/chat/completions"
    )


def test_responses_maps_images_and_json_schema():
    body = responses_request(
        [
            {
                "role": "user",
                "name": "user",
                "content": [
                    {"type": "text", "text": "Describe"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,AA",
                            "detail": "low",
                        },
                    },
                ],
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "result",
                "schema": {"type": "object"},
                "strict": True,
            },
        },
    )
    assert body["input"][0]["content"] == [
        {"type": "input_text", "text": "Describe"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,AA",
            "detail": "low",
        },
    ]
    assert body["text"]["format"]["name"] == "result"
    assert "json_schema" not in body["text"]["format"]
