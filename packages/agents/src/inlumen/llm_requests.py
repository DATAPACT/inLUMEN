"""Shared OpenAI request compatibility; other compatible providers stay opt-in.

Keep endpoint and parameter decisions here rather than in each caller. Unknown
models are left to the provider to validate; a model name is not proof of access.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# AutoGen serializes exceptions between its runtime tasks. Retain only the safe,
# typed provider failure in a request-local holder shared by those tasks.
_PROVIDER_FAILURE: ContextVar[list | None] = ContextVar(
    "provider_failure", default=None
)


@contextmanager
def provider_error_scope():
    failures: list = []
    token = _PROVIDER_FAILURE.set(failures)
    try:
        try:
            yield
        except Exception as exc:
            if failures:
                raise failures[-1] from exc
            raise
    finally:
        _PROVIDER_FAILURE.reset(token)


def remember_provider_error(error):
    failures = _PROVIDER_FAILURE.get()
    if failures is not None:
        failures.append(error)


@dataclass(frozen=True)
class RequestPolicy:
    api_format: str
    modern_tokens: bool
    reasoning: bool
    effort: str | None

    def parameters(
        self, max_tokens: int | None = None, *, temperature=None, top_p=None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if max_tokens is not None:
            field = (
                "max_output_tokens"
                if self.api_format == "responses"
                else "max_completion_tokens"
                if self.modern_tokens
                else "max_tokens"
            )
            result[field] = max_tokens
        if self.effort is not None:
            if self.api_format == "responses":
                result["reasoning"] = {"effort": self.effort}
            else:
                result["reasoning_effort"] = self.effort
        # Omit sampling parameters for reasoning models, including when effort
        # is unspecified (their default may enable reasoning).
        if not self.reasoning or self.effort == "none":
            if temperature is not None:
                result["temperature"] = temperature
            if top_p is not None:
                result["top_p"] = top_p
        return result


def request_policy(
    model: str,
    base_url: str,
    *,
    provider: str = "custom",
    api_format: str = "auto",
    reasoning_effort: str | None = None,
    tools: bool = False,
) -> RequestPolicy:
    if api_format not in {"auto", "chat_completions", "responses"}:
        raise ValueError("api_format must be auto, chat_completions, or responses.")
    hostname = urlsplit(base_url).hostname or ""
    # Regional OpenAI endpoints (for example eu.api.openai.com) have the same
    # request capabilities. Match the domain boundary, not a loose substring.
    direct = (
        hostname == "api.openai.com"
        or hostname.endswith(".api.openai.com")
        or provider == "openai"
    )
    name = model.lower()
    reasoning = direct and bool(re.match(r"^(gpt-[56](?:[.\-]|$)|o[134](?:-|$))", name))
    gpt6 = direct and name.startswith("gpt-6")
    responses_only = direct and ("-codex" in name or "-pro" in name)
    selected = (
        ("responses" if gpt6 or responses_only else "chat_completions")
        if api_format == "auto"
        else api_format
    )
    if selected == "chat_completions" and (
        responses_only
        or (gpt6 and tools and reasoning_effort != "none")
        or (
            tools
            and name.startswith(("gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra"))
            and direct
        )
    ):
        raise ValueError(
            f"Model {model} requires the Responses API for this request; use api_format=responses or auto."
        )
    if reasoning_effort is not None:
        if reasoning_effort not in {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("Unsupported reasoning_effort value.")
        if direct and not reasoning:
            raise ValueError(f"Model {model} does not support reasoning_effort.")
        if (
            direct
            and name.startswith(("gpt-6-astra", "gpt-6.1-sol"))
            and reasoning_effort == "none"
        ):
            raise ValueError(
                f"Model {model} does not support reasoning_effort=none; use low or higher."
            )
        if gpt6 and reasoning_effort == "minimal":
            raise ValueError(
                f"Model {model} does not support reasoning_effort=minimal; use low or higher."
            )
        allowed = None
        # Check specialized variants before their broader families. A dated
        # snapshot keeps its model's effort limits; model availability is still
        # validated by the provider.
        model_name = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", name)
        if direct and model_name == "gpt-5.5-pro":
            allowed = {"medium", "high", "xhigh"}
        elif direct and model_name == "gpt-5.2-codex":
            allowed = {"low", "medium", "high", "xhigh"}
        elif direct and name.startswith("gpt-5.5"):
            allowed = {"none", "low", "medium", "high", "xhigh"}
        elif direct and re.match(
            r"^gpt-5(?:-(?:mini|nano))?(?:-20\d\d-\d\d-\d\d)?$", name
        ):
            allowed = {"minimal", "low", "medium", "high"}
        elif direct and re.match(r"^o[134](?:-|$)", name):
            allowed = {"low", "medium", "high"}
        if allowed is not None and reasoning_effort not in allowed:
            raise ValueError(
                f"Model {model} does not support reasoning_effort={reasoning_effort}; supported values: {', '.join(sorted(allowed))}."
            )
    return RequestPolicy(
        selected,
        direct,
        reasoning or reasoning_effort not in {None, "none"},
        reasoning_effort,
    )


def endpoint_url(base_url: str, api_format: str) -> str:
    url = base_url.strip().rstrip("/")
    path = "/responses" if api_format == "responses" else "/chat/completions"
    for suffix in ("/chat/completions", "/responses"):
        if url.endswith(suffix):
            return url.removesuffix(suffix) + path
    if not url.endswith("/v1"):
        url += "/v1"
    return url + path



def responses_request(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    replay: Mapping[str, list] | None = None,
) -> dict:
    """Translate the agent's chat history to stateless Responses input items."""
    items = []
    for message in messages:
        role = message["role"]
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": message["content"],
                }
            )
            continue
        calls = message.get("tool_calls") or []
        # Reuse original output items (including encrypted reasoning) for tool
        # turns made by this client. Reconstructed history still works on reload.
        original = (replay or {}).get(calls[0]["id"]) if calls else None
        if original is not None:
            items.extend(original)
            continue
        content = message.get("content")
        if content:
            if isinstance(content, list):
                content = [
                    (
                        {"type": "input_text", "text": part["text"]}
                        if part["type"] == "text"
                        else {"type": "input_image", **part["image_url"]}
                        if part["type"] == "image_url"
                        else part
                    )
                    for part in content
                ]
                for part in content:
                    if part.get("type") == "input_image":
                        part["image_url"] = part.pop("url")
            items.append({"role": role, "content": content})
        for call in calls:
            items.append(
                {"type": "function_call", "call_id": call["id"], **call["function"]}
            )
    result: dict = {
        "input": items,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if tools:
        result["tools"] = [
            {
                "type": "function",
                **tool["function"],
                "strict": tool["function"].get("strict", False),
            }
            for tool in tools
        ]
    if response_format:
        fmt = dict(response_format)
        if fmt.get("type") == "json_schema":
            fmt = {"type": "json_schema", **fmt["json_schema"]}
        result["text"] = {"format": fmt}
    return result


def responses_usage(payload: dict, *, include_cost: bool = False) -> dict:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    counts = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    if include_cost and "cost" in usage:
        counts["cost"] = usage["cost"]
    return counts


def responses_result(payload: dict, *, secrets=(), include_cost: bool = False) -> tuple[str, list[dict], dict, str]:
    if payload.get("status") == "failed" or payload.get("error"):
        raise ProviderRequestError.from_body(502, payload, secrets=secrets)
    output = payload.get("output") or []
    status = payload.get("status")
    unfinished_item = any(
        item.get("status") in {"incomplete", "in_progress"} for item in output
    )
    # A parseable argument object or artifact can still belong to an unfinished
    # response. AutoGen executes tool-call content without checking finish_reason,
    # so reject it here, before either caller can accept or dispatch any output.
    if status not in {None, "completed"} or unfinished_item:
        details = payload.get("incomplete_details") or {}
        reason = details.get("reason") or (
            "unfinished_output" if unfinished_item else status
        )
        message = f"Model response is incomplete ({reason}). No unfinished output was accepted."
        if reason == "max_output_tokens":
            message += " Increase the output token limit before retrying."
        raise ProviderRequestError(
            502,
            redact_message(message, secrets),
            code="incomplete_response",
            param="max_output_tokens" if reason == "max_output_tokens" else None,
        )
    text, calls = [], []
    for item in output:
        if item.get("type") == "function_call":
            calls.append(item)
        elif item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text.append(part["text"])
                elif part.get("type") == "refusal":
                    text.append(part["refusal"])
    finish = "function_calls" if calls else "stop"
    return "".join(text), calls, responses_usage(payload, include_cost=include_cost), finish


def redact_message(message: str, secrets=()) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(
        r"(?i)Bearer\s+[^\s\"']+|\bsk-[A-Za-z0-9_-]+", "[redacted]", message
    )
    return message[:1000]


class ProviderRequestError(RuntimeError):
    def __init__(self, status_code: int, message: str, *, code=None, param=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.param = param

    @classmethod
    def from_body(cls, status: int, body: Any, *, secrets=()):
        error = body.get("error", body) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            error = {}
        return cls(
            status,
            redact_message(
                str(
                    error.get("message")
                    or f"Model provider request failed (HTTP {status})."
                ),
                secrets,
            ),
            code=redact_message(str(error["code"]), secrets)
            if error.get("code")
            else None,
            param=redact_message(str(error["param"]), secrets)
            if error.get("param")
            else None,
        )


def provider_error(exc: BaseException, *, secrets=()) -> ProviderRequestError | None:
    """Find typed provider failures even when an agent wraps the exception."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ProviderRequestError):
            return ProviderRequestError(
                exc.status_code,
                redact_message(str(exc), secrets),
                code=redact_message(exc.code, secrets) if exc.code else None,
                param=redact_message(exc.param, secrets) if exc.param else None,
            )
        # Avoid depending on the OpenAI SDK in the standalone codegen package.
        if (
            type(exc).__module__.startswith("openai")
            and type(exc).__name__ == "APITimeoutError"
        ):
            return ProviderRequestError(504, "Model provider request timed out.")
        if (
            type(exc).__module__.startswith("openai")
            and type(exc).__name__ == "APIConnectionError"
        ):
            return ProviderRequestError(502, "Could not connect to the model provider.")
        if type(exc).__module__.startswith("openai") and hasattr(exc, "status_code"):
            return ProviderRequestError.from_body(
                exc.status_code, getattr(exc, "body", None), secrets=secrets
            )
        exc = exc.__cause__ or exc.__context__
    return None
