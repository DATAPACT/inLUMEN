from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from inlumen.llm_requests import (
    ProviderRequestError, endpoint_url, request_policy, responses_request,
    responses_result, redact_message,
)
from inlumen.contracts.events import model_request_event
from .generation_budget import CURRENT_BUDGET
from .model import CURRENT_EVENTS, CURRENT_MODEL, ModelClient, ModelReply

from .schemas import (
    GenerateNodeScriptRequest,
    GenerationUsage,
    LLMConfig,
    ValidationReport,
)


class LLMGenerationError(RuntimeError):
    """Raised when the configured coding model cannot return a JSON artifact."""

    def __init__(self, message: str, *, status_code: int = 422, code=None, param=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.param = param


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


def _is_openrouter(provider: str) -> bool:
    return provider.strip().lower().replace("-", "_") in {"openrouter", "open_router"}


class HttpModelClient:
    """The default model client: an OpenAI-compatible chat-completions endpoint."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.name = config.model

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_output: bool,
        max_tokens: int,
    ) -> ModelReply:
        config = self.config
        try:
            policy = request_policy(
                config.model,
                config.base_url,
                provider=config.provider,
                api_format=config.api_format,
                reasoning_effort=config.reasoning_effort,
            )
        except ValueError as exc:
            raise LLMGenerationError(str(exc), status_code=400) from exc
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        fmt = {"type": "json_object"} if json_output else None
        body: dict[str, Any] = {"model": config.model}
        body.update(
            policy.parameters(
                max_tokens, temperature=config.temperature, top_p=config.top_p
            )
        )
        if policy.api_format == "responses":
            body.update(responses_request(messages, response_format=fmt))
        else:
            body["messages"] = messages
            if fmt:
                body["response_format"] = fmt
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if _is_openrouter(config.provider):
            headers.update(_openrouter_headers())
            if config.openrouter_provider_only:
                body["provider"] = {"only": config.openrouter_provider_only}
        timeout = httpx.Timeout(max(1, config.timeout_seconds))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    endpoint_url(config.base_url, policy.api_format),
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                error_body = exc.response.json()
            except ValueError:
                error_body = {}
            error = ProviderRequestError.from_body(
                exc.response.status_code, error_body, secrets=(config.api_key,)
            )
            raise LLMGenerationError(
                str(error),
                status_code=error.status_code,
                code=error.code,
                param=error.param,
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMGenerationError(
                "Coding model request timed out.", status_code=504
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMGenerationError(
                "Could not connect to the coding model provider.", status_code=502
            ) from exc

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
        if policy.api_format == "responses":
            try:
                content, calls, usage, finish = responses_result(response_payload)
            except ProviderRequestError as exc:
                raise LLMGenerationError(
                    redact_message(str(exc), (config.api_key,)),
                    status_code=exc.status_code,
                    code=exc.code,
                    param=exc.param,
                ) from exc
            if calls or not content:
                raise LLMGenerationError(
                    "Coding model returned no text artifact.", status_code=502
                )
            return ModelReply(
                content=content, usage=usage, finish_reason=finish, raw=response_payload
            )
        try:
            choice = response_payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMGenerationError(
                "Coding model returned an unsupported chat-completions response."
            ) from exc
        usage = response_payload.get("usage")
        if isinstance(usage, dict) and not _is_openrouter(config.provider):
            # Only OpenRouter reports a cost we can trust as USD.
            usage = {key: value for key, value in usage.items() if key != "cost"}
        return ModelReply(
            content=content,
            usage=usage if isinstance(usage, dict) else None,
            finish_reason=choice.get("finish_reason")
            if isinstance(choice, dict)
            else None,
            raw=response_payload,
        )


def _check_endpoint(config: LLMConfig | None) -> None:
    if config is None or not config.model.strip():
        raise LLMGenerationError("Code-generation model is not configured.")
    if not config.base_url.strip():
        raise LLMGenerationError("Code-generation provider base URL is not configured.")
    if not config.api_key.strip():
        raise LLMGenerationError("Code-generation provider API key is missing.")


async def generate_json(
    config: LLMConfig | None,
    *,
    system_prompt: str,
    user_prompt: str,
    usage_callback: UsageCallback | None = None,
) -> dict[str, Any]:
    """Ask the model for one JSON object.

    The request goes to the client installed with
    :func:`inlumen.codegen.model.use_model`, else to ``config``'s endpoint.
    Every request counts against the run's budget and is reported to the
    usage callback and the event sink.
    """
    client = CURRENT_MODEL.get()
    if client is None:
        _check_endpoint(config)
        client = HttpModelClient(config)
    elif isinstance(client, HttpModelClient):
        _check_endpoint(client.config)

    budget = CURRENT_BUDGET.get()
    if budget is not None: budget.reserve()
    events = CURRENT_EVENTS.get()
    started = time.monotonic()
    try:
        reply = await client.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_output=config.supports_json_output if config is not None else True,
            max_tokens=config.max_output_tokens if config is not None else 16384,
        )
    except BaseException as exc:
        if events is not None:
            events(_request_event(client, started, None, exc))
        raise
    if events is not None:
        events(_request_event(client, started, reply, None))
    usage = _usage_from_response({"usage": reply.usage}, include_usd_cost=True)
    if budget is not None:
        budget.reported_cost_usd += usage.cost_usd or 0
    if usage_callback is not None:
        callback_result = usage_callback(usage.model_copy())
        if inspect.isawaitable(callback_result):
            await callback_result
    return _json_from_model_content(reply.content)


def _request_event(
    client: ModelClient,
    started: float,
    reply: ModelReply | None,
    error: BaseException | None,
) -> dict[str, Any]:
    usage = (reply.usage if reply is not None else None) or {}
    return model_request_event(
        purpose="codegen",
        model=getattr(client, "name", ""),
        prompt_tokens=_optional_nonnegative_number(usage.get("prompt_tokens"), int),
        completion_tokens=_optional_nonnegative_number(usage.get("completion_tokens"), int),
        cost_usd=_optional_nonnegative_number(usage.get("cost"), float),
        latency_s=time.monotonic() - started,
        finish_reason=reply.finish_reason if reply is not None else None,
        error=f"{type(error).__name__}: {error}" if error else None,
    )


NODE_SYSTEM_PROMPT = """You are the inLumen production code-generation engine.
Generate executable Python runtime code from the supplied node context.
Return exactly one JSON object with: main_py (string), requirements (array of
PEP 508 strings), outputs (array), implementation_plan (object), and notes
(array of strings). Never return Markdown or a Dockerfile. The platform starts
main.py in a standard workspace. The program must read upstream files directly
from PIPELINE_INPUT_DIR and write every downstream artifact directly beneath
PIPELINE_OUTPUT_DIR. Port names never create implicit workspace directories.
Use the input descriptors in INLUMEN_INPUT_MANIFEST to open their explicit paths.
There is exactly one descriptor per incoming connection, identified by
consumer target_port and declared artifact name or filename. Runtime connection_id and source_node are diagnostic identities; do not hard-code their graph-specific values. Never discover inputs by scanning a directory or
selecting the first matching file. Only the declared output artifacts are handed
to the next steps; platform metadata lives outside them.
Regular node
parameters are available under their exact safe parameter names (for example,
QUESTION), as JSON in PIPELINE_PARAMS_JSON, and individually as
PIPELINE_PARAM_<NAME>. Preserve deliberate artifact-relative paths but do not
depend on the source connector or an orchestration port name. Sensitive
parameter names are listed in target_node.secret_parameters; their values are
available only from the corresponding PIPELINE_PARAM_<NAME> environment variable
at runtime. Never print, persist, or return a sensitive value. Publish every
declared output artifact, and nothing else, at its declared filename beneath
PIPELINE_OUTPUT_DIR. Usually there is one; when several are declared, each
names the output port it feeds and the platform routes it there, so write
each at its own declared path and never create port folders yourself.
Keep scratch files in PIPELINE_WORK_DIR. Use only allowed packages and honor reviewed
implementation constraints. Do not use subprocess, eval, exec, os.system, or
undeclared network access."""


PIPELINE_SYSTEM_PROMPT = """You are the inLumen production pipeline code-generation engine.
Generate one canonical Python module for the supplied pipeline plan. Return
exactly one JSON object with pipeline_py (string), requirements (array of PEP
508 strings), and notes (array of strings). Never return Markdown or a
Dockerfile. Define every requested node function exactly once with signature
(inputs, output_dir, context). Functions must be synchronous, must not call
other node functions, must materialize their declared artifacts in output_dir,
and must return a list with one descriptor per declared output, in any order
(terminal destinations may return no artifact). Most nodes declare one output;
a node with several declares a distinct port on each. A directory bundle is
one descriptor.
Inputs are explicitly bound by the runtime. Select by consumer target_port and,
when needed, declared artifact name or filename. Never hard-code graph-specific
connection_id or source_node values; they are diagnostic identities. Select the bound
input; never scan for the first matching file or assume a bundle is multiple inputs. Top-level code may
contain imports, literal constants, classes, and function definitions only.
Every input is a descriptor dictionary. Read its `path` value; never assume a
descriptor `filename` exists in the process working directory and never open a
hard-coded input filename directly. Read node parameters from
context["parameters"]. Boundary source and destination functions
may be replaced by compiler-owned adapters, while task functions remain the
coding model's implementation.
For transcript JSON, transcript is the canonical downstream text field. Transformations
that change text must keep any text aliases and segment text consistent or remove stale
aliases. Never let consumers select an original alias in preference to a transformed transcript.
Keep the module concise and return complete, valid Python: close every string,
bracket, call, and function body. The requirements array may contain only
package strings copied from pipeline_plan.required_packages or the supplied
allowed package list; never return numbers, bullets, prose, or invented
packages. Use only allowed packages; do not use subprocess, sockets, eval,
exec, os.system, or undeclared network access."""


def without_validation_bytes(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_validation_bytes(item) for key, item in value.items()
                if key != "content_base64"}
    if isinstance(value, list):
        return [without_validation_bytes(item) for item in value]
    return value


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
        user_prompt=json.dumps(without_validation_bytes(prompt), ensure_ascii=False),
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
        user_prompt=json.dumps(without_validation_bytes(prompt), ensure_ascii=False),
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
        user_prompt=json.dumps(without_validation_bytes(prompt), ensure_ascii=False),
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
        user_prompt=json.dumps(without_validation_bytes(prompt), ensure_ascii=False),
        usage_callback=usage_callback,
    )
