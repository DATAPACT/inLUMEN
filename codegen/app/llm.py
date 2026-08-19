from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .schemas import (
    GenerateNodeScriptRequest,
    GenerationUsage,
    LLMConfig,
    ValidationReport,
)


class LLMGenerationError(RuntimeError):
    """Raised when the configured coding model cannot return a JSON artifact."""


UsageCallback = Callable[[GenerationUsage], Awaitable[None] | None]


def _openrouter_headers() -> dict[str, str]:
    return {
        "HTTP-Referer": "https://github.com/DATAPACT/inLUMEN",
        "X-OpenRouter-Title": "inLUMEN",
    }


def _optional_nonnegative_number(value: Any, cast: type[int] | type[float]):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = cast(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _usage_from_response(
    payload: dict[str, Any],
    *,
    include_usd_cost: bool,
) -> GenerationUsage:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return GenerationUsage(request_count=1)
    return GenerationUsage(
        request_count=1,
        usage_reported_count=1,
        prompt_tokens=_optional_nonnegative_number(raw.get("prompt_tokens"), int),
        completion_tokens=_optional_nonnegative_number(
            raw.get("completion_tokens"), int
        ),
        total_tokens=_optional_nonnegative_number(raw.get("total_tokens"), int),
        cost_usd=(
            _optional_nonnegative_number(raw.get("cost"), float)
            if include_usd_cost
            else None
        ),
    )


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _json_from_model_content(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError(
            f"Coding model returned invalid JSON: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise LLMGenerationError("Coding model response must be a JSON object.")
    return payload


async def generate_json(
    config: LLMConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    usage_callback: UsageCallback | None = None,
) -> dict[str, Any]:
    if not config.model.strip():
        raise LLMGenerationError("Code-generation model is not configured.")
    if not config.base_url.strip():
        raise LLMGenerationError("Code-generation provider base URL is not configured.")
    if not config.api_key.strip():
        raise LLMGenerationError("Code-generation provider API key is missing.")

    body: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        # Canonical pipelines can contain several independently compiled node
        # functions. Give the coding model enough room to finish the JSON
        # envelope and every function instead of returning a syntactically
        # truncated module for larger graphs.
        "max_tokens": 16384,
    }
    if config.supports_json_output:
        body["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if config.provider.strip().lower().replace("-", "_") in {
        "openrouter",
        "open_router",
    }:
        headers.update(_openrouter_headers())
    timeout = httpx.Timeout(max(1, config.timeout_seconds))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                _chat_completions_url(config.base_url),
                headers=headers,
                json=body,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:1000]
        raise LLMGenerationError(
            f"Coding model request failed ({exc.response.status_code}): {detail}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMGenerationError(f"Coding model request failed: {exc}") from exc

    try:
        response_payload = response.json()
    except ValueError as exc:
        raise LLMGenerationError(
            "Coding model returned an unsupported chat-completions response."
        ) from exc
    if not isinstance(response_payload, dict):
        raise LLMGenerationError(
            "Coding model returned an unsupported chat-completions response."
        )
    if usage_callback is not None:
        callback_result = usage_callback(
            _usage_from_response(
                response_payload,
                include_usd_cost=(
                    config.provider.strip().lower().replace("-", "_")
                    in {"openrouter", "open_router"}
                ),
            )
        )
        if inspect.isawaitable(callback_result):
            await callback_result
    try:
        content = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMGenerationError(
            "Coding model returned an unsupported chat-completions response."
        ) from exc
    return _json_from_model_content(content)


NODE_SYSTEM_PROMPT = """You are the inLumen production code-generation engine.
Generate executable Python runtime code from the supplied node context.
Return exactly one JSON object with: main_py (string), requirements (array of
PEP 508 strings), outputs (array), implementation_plan (object), and notes
(array of strings). Never return Markdown or a Dockerfile. The platform starts
main.py in a standard workspace. The program must read input files only from
PIPELINE_INPUT_DIR (recursively, including port subdirectories) and write every
downstream artifact only beneath PIPELINE_OUTPUT_DIR. Files written there are
the complete hand-off to downstream nodes; do not depend on metadata files.
Regular node
parameters are available under their exact safe parameter names (for example,
QUESTION), as JSON in PIPELINE_PARAMS_JSON, and individually as
PIPELINE_PARAM_<NAME>. Inputs may be nested under port directories; discover
files recursively and do not depend on the source connector. Sensitive
parameter names are listed in target_node.secret_parameters; their values are
available only from the corresponding PIPELINE_PARAM_<NAME> environment variable
at runtime. Never print, persist, or return a sensitive value. Files themselves
are the complete hand-off. Write artifacts beneath output_dir, optionally
grouped by output port. Use only allowed packages and honor reviewed
implementation constraints. Do not use subprocess, eval, exec, os.system, or
undeclared network access."""


PIPELINE_SYSTEM_PROMPT = """You are the inLumen production pipeline code-generation engine.
Generate one canonical Python module for the supplied pipeline plan. Return
exactly one JSON object with pipeline_py (string), requirements (array of PEP
508 strings), and notes (array of strings). Never return Markdown or a
Dockerfile. Define every requested node function exactly once with signature
(inputs, output_dir, context). Functions must be synchronous, must not call
other node functions, must materialize their declared artifacts in output_dir,
and must return a list of output descriptor dictionaries. Top-level code may
contain imports, literal constants, classes, and function definitions only.
Every input is a descriptor dictionary. Read its `path` value; never assume a
descriptor `filename` exists in the process working directory and never open a
hard-coded input filename directly. Read node parameters from
context["parameters"]. Boundary source and destination functions
may be replaced by compiler-owned adapters, while task functions remain the
coding model's implementation.
Keep the module concise and return complete, valid Python: close every string,
bracket, call, and function body. The requirements array may contain only
package strings copied from pipeline_plan.required_packages or the supplied
allowed package list; never return numbers, bullets, prose, or invented
packages. Use only allowed packages; do not use subprocess, sockets, eval,
exec, os.system, or undeclared network access."""


async def generate_node_payload(
    config: LLMConfig,
    request: GenerateNodeScriptRequest,
    usage_callback: UsageCallback | None = None,
) -> dict[str, Any]:
    context = request.context.model_dump(mode="json")
    prompt = {
        "operation": "generate_node_runtime",
        "context": context,
        "user_instruction": request.options.user_instruction,
    }
    return await generate_json(
        config,
        system_prompt=NODE_SYSTEM_PROMPT,
        user_prompt=json.dumps(prompt, ensure_ascii=False),
        usage_callback=usage_callback,
    )


async def repair_node_payload(
    config: LLMConfig,
    request: GenerateNodeScriptRequest,
    payload: dict[str, Any],
    validation: ValidationReport,
    usage_callback: UsageCallback | None = None,
) -> dict[str, Any]:
    prompt = {
        "operation": "repair_node_runtime",
        "context": request.context.model_dump(mode="json"),
        "user_instruction": request.options.user_instruction,
        "previous_payload": payload,
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
    }
    return await generate_json(
        config,
        system_prompt=NODE_SYSTEM_PROMPT,
        user_prompt=json.dumps(prompt, ensure_ascii=False),
        usage_callback=usage_callback,
    )


async def generate_pipeline_payload(
    config: LLMConfig,
    plan: dict[str, Any],
    user_instruction: str,
    usage_callback: UsageCallback | None = None,
) -> dict[str, Any]:
    prompt = {
        "operation": "generate_canonical_pipeline",
        "pipeline_plan": plan,
        "user_instruction": user_instruction,
    }
    return await generate_json(
        config,
        system_prompt=PIPELINE_SYSTEM_PROMPT,
        user_prompt=json.dumps(prompt, ensure_ascii=False),
        usage_callback=usage_callback,
    )


async def repair_pipeline_payload(
    config: LLMConfig,
    plan: dict[str, Any],
    payload: dict[str, Any],
    validation: ValidationReport,
    user_instruction: str,
    usage_callback: UsageCallback | None = None,
) -> dict[str, Any]:
    prompt = {
        "operation": "repair_canonical_pipeline",
        "pipeline_plan": plan,
        "user_instruction": user_instruction,
        "previous_payload": payload,
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
    }
    return await generate_json(
        config,
        system_prompt=PIPELINE_SYSTEM_PROMPT,
        user_prompt=json.dumps(prompt, ensure_ascii=False),
        usage_callback=usage_callback,
    )
