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
    responses_result, responses_usage,
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

    def __init__(self, message: str, *, status_code: int = 422, code=None, param=None, usage=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.param = param
        self.usage = usage


UsageCallback = Callable[[GenerationUsage], Awaitable[None] | None]

# One retry when a reply is cut off or is not a JSON object.
REPLY_ATTEMPTS = 2
MAX_OUTPUT_TOKENS = 32768
CUT_OFF_FINISH_REASONS = {"length", "max_tokens"}


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


def _reply_text(content: Any) -> str:
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "").strip()


_FENCED_BLOCK = re.compile(
    r"^```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)^```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
_PYTHON_FENCES = {"python", "py", "python3"}
_NODE_HEADING = re.compile(
    r"^#{2,}[ \t]*node[ \t:]+(\S+)[ \t]*\r?$", re.MULTILINE | re.IGNORECASE
)


def _json_block(text: str) -> dict[str, Any]:
    """The object in the reply's first ```json block; {} when there is none."""
    for language, body in _FENCED_BLOCK.findall(text):
        if language.lower() != "json":
            continue
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMGenerationError(
                f"Coding model returned invalid JSON in its json block: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise LLMGenerationError("The json block must hold one JSON object.")
        return value
    return {}


def code_reply_parser(code_key: str) -> Callable[[Any], dict[str, Any]]:
    """Parse a reply of one ```json block and ```python code into a payload.

    The code is stored under ``code_key``, so the payload has the same shape
    as a JSON-mode reply.
    """

    def parse(content: Any) -> dict[str, Any]:
        text = _reply_text(content)
        code = [
            body for language, body in _FENCED_BLOCK.findall(text)
            if language.lower() in _PYTHON_FENCES
        ]
        if not code:
            raise LLMGenerationError(
                "Coding model reply has no complete ```python block"
            )
        return {**_json_block(text), code_key: "\n\n\n".join(code)}

    return parse


def _node_batch_from_code_reply(content: Any) -> dict[str, Any]:
    """Parse '## node <flow_id>' headings, each followed by a ```python block."""
    text = _reply_text(content)
    described = _json_block(text).get("nodes")
    described = described if isinstance(described, dict) else {}
    headings = list(_NODE_HEADING.finditer(text))
    nodes: dict[str, Any] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        code = [
            body for language, body in _FENCED_BLOCK.findall(text[heading.end():end])
            if language.lower() in _PYTHON_FENCES
        ]
        if code:
            flow_id = heading.group(1).strip("`*:")
            details = described.get(flow_id)
            nodes[flow_id] = {
                **(details if isinstance(details, dict) else {}),
                "main_py": code[0],
            }
    if not nodes:
        raise LLMGenerationError(
            "Coding model reply has no '## node <flow_id>' heading with a ```python block"
        )
    return {"nodes": nodes}


def _json_from_model_content(content: Any) -> dict[str, Any]:
    text = _reply_text(content)
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
            include_cost = _is_openrouter(config.provider)
            try:
                content, calls, usage, finish = responses_result(
                    response_payload, secrets=(config.api_key,), include_cost=include_cost
                )
            except ProviderRequestError as exc:
                usage = None
                if isinstance(response_payload.get("usage"), dict):
                    usage = responses_usage(response_payload, include_cost=include_cost)
                raise LLMGenerationError(
                    str(exc),
                    status_code=exc.status_code,
                    code=exc.code,
                    param=exc.param,
                    usage=usage,
                ) from exc
            if calls or not content:
                raise LLMGenerationError(
                    "Coding model returned no text artifact.", status_code=502, usage=usage
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
    parse: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask the model for one payload: a JSON object unless ``parse`` is given.

    ``parse`` turns the reply into the payload when the code is not wrapped
    in JSON (see :func:`code_reply_parser`); JSON mode is then not requested.

    The request goes to the client installed with
    :func:`inlumen.codegen.model.use_model`, else to ``config``'s endpoint.
    Every request counts against the run's budget and is reported to the
    usage callback and the event sink. A reply that was cut off at the output
    limit, or that is not a JSON object, is requested once more: with a
    doubled output limit, or with the parse error added to the system prompt.
    """
    client = CURRENT_MODEL.get()
    if client is None:
        _check_endpoint(config)
        client = HttpModelClient(config)
    elif isinstance(client, HttpModelClient):
        _check_endpoint(client.config)

    budget = CURRENT_BUDGET.get()
    events = CURRENT_EVENTS.get()
    max_tokens = config.max_output_tokens if config is not None else 16384
    retry_note = ""
    for attempt in range(REPLY_ATTEMPTS):
        last = attempt == REPLY_ATTEMPTS - 1
        if budget is not None: budget.reserve()
        started = time.monotonic()
        try:
            reply = await client.complete(
                system_prompt=system_prompt + retry_note,
                user_prompt=user_prompt,
                json_output=parse is None
                and (config.supports_json_output if config is not None else True),
                max_tokens=max_tokens,
            )
        except BaseException as exc:
            raw_usage = exc.usage if isinstance(exc, LLMGenerationError) else None
            if raw_usage is not None:
                await _record_usage(raw_usage, usage_callback)
            if events is not None:
                events(_request_event(client, started, raw_usage, error=exc))
            if last or not _output_limit_reached(exc):
                raise
        else:
            if events is not None:
                events(_request_event(client, started, reply.usage, finish_reason=reply.finish_reason))
            await _record_usage(reply.usage, usage_callback)
            if reply.finish_reason not in CUT_OFF_FINISH_REASONS:
                try:
                    return (parse or _json_from_model_content)(reply.content)
                except LLMGenerationError as exc:
                    if last:
                        raise
                    retry_note = (
                        f"\n\nYour previous reply was rejected: {str(exc).rstrip('.')}. Reply again "
                        + ("in the required format, complete." if parse else
                           "with one complete JSON object and nothing else.")
                    )
                    continue
            if last:
                raise LLMGenerationError(
                    "Coding model reply was cut off at the output limit of "
                    f"{max_tokens} tokens. No unfinished output was accepted.",
                    status_code=502,
                    code="incomplete_response",
                    param="max_output_tokens",
                )
        # The reply was cut off: allow a longer one, or ask for a shorter one
        # when the limit cannot grow.
        larger = min(MAX_OUTPUT_TOKENS, max_tokens * 2)
        if larger > max_tokens:
            max_tokens, retry_note = larger, ""
        else:
            retry_note = (
                "\n\nYour previous reply was cut off at the output limit of "
                f"{max_tokens} tokens. Reply again with a shorter, complete reply."
            )
    raise AssertionError("unreachable")


def _output_limit_reached(exc: BaseException) -> bool:
    return (
        isinstance(exc, LLMGenerationError)
        and exc.code == "incomplete_response"
        and exc.param == "max_output_tokens"
    )


async def _record_usage(raw_usage: dict | None, callback: UsageCallback | None) -> None:
    usage = _usage_from_response({"usage": raw_usage}, include_usd_cost=True)
    budget = CURRENT_BUDGET.get()
    if budget is not None:
        budget.reported_cost_usd += usage.cost_usd or 0
    if callback is not None:
        callback_result = callback(usage)
        if inspect.isawaitable(callback_result):
            await callback_result


def _request_event(
    client: ModelClient,
    started: float,
    raw_usage: dict | None,
    *,
    finish_reason: str | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    usage = raw_usage or {}
    return model_request_event(
        purpose="codegen",
        model=getattr(client, "name", ""),
        prompt_tokens=_optional_nonnegative_number(usage.get("prompt_tokens"), int),
        completion_tokens=_optional_nonnegative_number(usage.get("completion_tokens"), int),
        cost_usd=_optional_nonnegative_number(usage.get("cost"), float),
        latency_s=time.monotonic() - started,
        finish_reason=finish_reason,
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
INLUMEN_INPUT_MANIFEST holds the path of a JSON file, not JSON text and not a
file name inside the input folder. Read it exactly like this:
with open(os.environ["INLUMEN_INPUT_MANIFEST"]) as handle: inputs = json.load(handle)["inputs"]
The platform sets only PIPELINE_INPUT_DIR, PIPELINE_OUTPUT_DIR,
PIPELINE_WORK_DIR, INLUMEN_INPUT_MANIFEST and the parameter variables named
below; do not read any other environment variable. Each descriptor has filename, path,
target_port, source_node, kind and format. Open the file at the descriptor's
path value; it is PIPELINE_INPUT_DIR joined with the descriptor's filename,
which may include a folder. The manifest has one descriptor for each entry of
context.available_inputs, with exactly the same filename and target_port: find
the descriptor of an input by comparing its filename with that entry's
filename. Do not match on a name you derive, on source_node or on a file name
without its folder. No output manifest is required: the platform
finds the declared files in PIPELINE_OUTPUT_DIR.
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
(terminal destinations may return no artifact). In a returned descriptor,
filename is the declared file name exactly as declared, never a path. Most nodes declare one output;
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


# The reply format is the only part of a system prompt that depends on
# GenerationOptions.reply_format. "code_block" keeps the code out of a JSON
# string, so the model does not have to escape quotes and newlines.
_NODE_JSON_REPLY = """Return exactly one JSON object with: main_py (string), requirements (array of
PEP 508 strings), outputs (array), implementation_plan (object), and notes
(array of strings). Never return Markdown or a Dockerfile."""
_NODE_CODE_REPLY = """Reply with one ```json block holding an object with requirements (array of
PEP 508 strings), outputs (array), implementation_plan (object) and notes
(array of strings), followed by one ```python block holding the complete
main.py. Write the code as plain Python, never as a JSON string. Never return
a Dockerfile."""
_PIPELINE_JSON_REPLY = """Return
exactly one JSON object with pipeline_py (string), requirements (array of PEP
508 strings), and notes (array of strings). Never return Markdown or a
Dockerfile."""
_PIPELINE_CODE_REPLY = """Reply
with one ```json block holding an object with requirements (array of PEP 508
strings) and notes (array of strings), followed by one ```python block holding
the complete module. Write the code as plain Python, never as a JSON string.
Never return a Dockerfile."""
assert _NODE_JSON_REPLY in NODE_SYSTEM_PROMPT and _PIPELINE_JSON_REPLY in PIPELINE_SYSTEM_PROMPT
NODE_CODE_SYSTEM_PROMPT = NODE_SYSTEM_PROMPT.replace(_NODE_JSON_REPLY, _NODE_CODE_REPLY)
PIPELINE_CODE_SYSTEM_PROMPT = PIPELINE_SYSTEM_PROMPT.replace(
    _PIPELINE_JSON_REPLY, _PIPELINE_CODE_REPLY
)

# Instructions for one operation go in the user prompt, after the plan, so the
# system prompt and the plan stay the same from the first draft to the last
# repair and a provider can reuse them from its prompt cache.
_NODE_REPAIR_INSTRUCTIONS = {
    "json": (
        "This request repairs an existing module. previous_pipeline_py is the "
        "complete module; only the node functions named in repair_functions "
        "failed validation. Instead of the usual reply, return exactly one JSON "
        "object with functions (an object that maps each name in "
        "repair_functions to the complete new source of that function, starting "
        "at its def line), helpers_py (a string of new imports and new helper "
        "definitions the repaired functions need, or an empty string), "
        "requirements (the full array for the whole module) and notes. Do not "
        "return pipeline_py. Every other function stays as it is: do not "
        "redefine or rename an existing top-level name, and give any new helper "
        "a new name."
    ),
    "code_block": (
        "This request repairs an existing module. previous_pipeline_py is the "
        "complete module; only the node functions named in repair_functions "
        "failed validation. In the ```python block return only the complete new "
        "definition of each function named in repair_functions, plus any new "
        "imports and new helper definitions they need. Do not return the rest of "
        "the module. Every other function stays as it is: do not redefine or "
        "rename an existing top-level name, and give any new helper a new name. "
        "The json block holds the full requirements array for the whole module."
    ),
}
_NODE_BATCH_INSTRUCTIONS = {
    "json": (
        "This request generates several nodes at once. Each entry of nodes is "
        "the context of one node; graph and pipeline are shared. Instead of the "
        "usual reply, return exactly one JSON object with nodes: an object that "
        "maps every flow_id in nodes to the usual object for that node (main_py, "
        "requirements, outputs, implementation_plan, notes). Each main.py is a "
        "separate program and shares no code with the others."
    ),
    "code_block": (
        "This request generates several nodes at once. Each entry of nodes is "
        "the context of one node; graph and pipeline are shared. Instead of the "
        "usual reply, write one ```json block holding an object with nodes: an "
        "object that maps every flow_id to requirements, outputs, "
        "implementation_plan and notes for that node. Then, for every node, "
        "write a line '## node <flow_id>' followed by one ```python block "
        "holding its complete main.py. Each main.py is a separate program and "
        "shares no code with the others."
    ),
}
MAX_HISTORY_ATTEMPTS = 3
MAX_HISTORY_ERROR_CHARS = 1500


def without_validation_bytes(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_validation_bytes(item) for key, item in value.items()
                if key != "content_base64"}
    if isinstance(value, list):
        return [without_validation_bytes(item) for item in value]
    return value


def attempt_record(attempt: int, validation: ValidationReport) -> dict[str, Any]:
    """What a later repair is told about an attempt that failed validation."""
    return {
        "attempt": attempt,
        "validation_errors": [
            error if len(error) <= MAX_HISTORY_ERROR_CHARS
            else "[earlier text omitted] " + error[-MAX_HISTORY_ERROR_CHARS:]
            for error in validation.errors
        ],
    }


def _history(earlier_attempts: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The attempts before the one being repaired, oldest first."""
    if not earlier_attempts:
        return {}
    return {
        "earlier_attempts_note": (
            "These earlier drafts also failed. Do not bring their errors back."
        ),
        "earlier_attempts": earlier_attempts[-MAX_HISTORY_ATTEMPTS:],
    }


async def _ask(
    config: LLMConfig,
    prompt: dict[str, Any],
    usage_callback: UsageCallback | None,
    *,
    json_system_prompt: str,
    code_system_prompt: str,
    reply_format: str,
    code_key: str,
    parse: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    code_reply = reply_format == "code_block"
    return await generate_json(
        config,
        system_prompt=code_system_prompt if code_reply else json_system_prompt,
        user_prompt=json.dumps(without_validation_bytes(prompt), ensure_ascii=False),
        usage_callback=usage_callback,
        parse=(parse or code_reply_parser(code_key)) if code_reply else None,
    )


async def _ask_node(config, prompt, usage_callback, reply_format, parse=None):
    return await _ask(
        config, prompt, usage_callback, reply_format=reply_format, code_key="main_py",
        json_system_prompt=NODE_SYSTEM_PROMPT, code_system_prompt=NODE_CODE_SYSTEM_PROMPT,
        parse=parse,
    )


async def _ask_pipeline(config, prompt, usage_callback, reply_format, code_key="pipeline_py"):
    return await _ask(
        config, prompt, usage_callback, reply_format=reply_format, code_key=code_key,
        json_system_prompt=PIPELINE_SYSTEM_PROMPT,
        code_system_prompt=PIPELINE_CODE_SYSTEM_PROMPT,
    )


async def generate_node_payload(
    config: LLMConfig,
    request: GenerateNodeScriptRequest,
    usage_callback: UsageCallback | None = None,
) -> dict[str, Any]:
    prompt = {
        "context": request.context.model_dump(mode="json"),
        "user_instruction": request.options.user_instruction,
        "operation": "generate_node_runtime",
    }
    return await _ask_node(config, prompt, usage_callback, request.options.reply_format)


async def generate_node_payloads(
    config: LLMConfig,
    requests: list[GenerateNodeScriptRequest],
    usage_callback: UsageCallback | None = None,
) -> dict[str, dict[str, Any]]:
    """First drafts for several nodes of one pipeline in one request.

    Returns the payloads by flow_id; a node the reply left out is missing.
    """
    first = requests[0]
    shared = first.context.model_dump(mode="json", include={"graph", "pipeline"})
    for key in ("upstream_nodes", "downstream_nodes"):
        shared["graph"].pop(key, None)
    for key in ("upstream_contracts", "downstream_contracts"):
        shared["pipeline"].pop(key, None)
    nodes = []
    for request in requests:
        context = request.context.model_dump(mode="json", exclude={"graph"})
        context["upstream_nodes"] = request.context.graph.upstream_nodes
        context["downstream_nodes"] = request.context.graph.downstream_nodes
        context["pipeline"] = {
            key: value for key, value in context["pipeline"].items()
            if key in ("upstream_contracts", "downstream_contracts")
        }
        nodes.append(context)
    reply_format = first.options.reply_format
    prompt = {
        **shared,
        "user_instruction": first.options.user_instruction,
        "operation": "generate_node_runtimes",
        "instructions": _NODE_BATCH_INSTRUCTIONS[reply_format],
        "nodes": nodes,
    }
    payload = await _ask_node(
        config, prompt, usage_callback, reply_format, parse=_node_batch_from_code_reply
    )
    drafts = payload.get("nodes")
    if not isinstance(drafts, dict):
        raise LLMGenerationError("Node batch reply is missing the nodes object.")
    wanted = {request.context.target_node.flow_id for request in requests}
    return {
        str(flow_id): draft for flow_id, draft in drafts.items()
        if str(flow_id) in wanted and isinstance(draft, dict)
        and str(draft.get("main_py") or "").strip()
    }


async def repair_node_payload(
    config: LLMConfig,
    request: GenerateNodeScriptRequest,
    payload: dict[str, Any],
    validation: ValidationReport,
    usage_callback: UsageCallback | None = None,
    earlier_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prompt = {
        "context": request.context.model_dump(mode="json"),
        "user_instruction": request.options.user_instruction,
        "operation": "repair_node_runtime",
        **_history(earlier_attempts),
        "previous_payload": payload,
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
    }
    return await _ask_node(config, prompt, usage_callback, request.options.reply_format)


async def generate_pipeline_payload(
    config: LLMConfig,
    plan: dict[str, Any],
    user_instruction: str,
    usage_callback: UsageCallback | None = None,
    reply_format: str = "json",
) -> dict[str, Any]:
    prompt = {
        "pipeline_plan": plan,
        "user_instruction": user_instruction,
        "operation": "generate_canonical_pipeline",
    }
    return await _ask_pipeline(config, prompt, usage_callback, reply_format)


async def repair_pipeline_payload(
    config: LLMConfig,
    plan: dict[str, Any],
    payload: dict[str, Any],
    validation: ValidationReport,
    user_instruction: str,
    usage_callback: UsageCallback | None = None,
    reply_format: str = "json",
    earlier_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prompt = {
        "pipeline_plan": plan,
        "user_instruction": user_instruction,
        "operation": "repair_canonical_pipeline",
        **_history(earlier_attempts),
        "previous_payload": payload,
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
    }
    return await _ask_pipeline(config, prompt, usage_callback, reply_format)


async def repair_pipeline_nodes_payload(
    config: LLMConfig,
    plan: dict[str, Any],
    payload: dict[str, Any],
    repair_functions: dict[str, str],
    validation: ValidationReport,
    user_instruction: str,
    usage_callback: UsageCallback | None = None,
    reply_format: str = "json",
    earlier_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask for new source of only the failing node functions.

    ``repair_functions`` maps each failing flow_id to its function name. A
    "json" reply holds ``functions`` and ``helpers_py``; a "code_block" reply
    holds ``functions_py``, the new definitions and helpers as one source.
    """
    prompt = {
        "pipeline_plan": plan,
        "user_instruction": user_instruction,
        "operation": "repair_pipeline_nodes",
        "instructions": _NODE_REPAIR_INSTRUCTIONS[reply_format],
        **_history(earlier_attempts),
        "previous_pipeline_py": payload.get("pipeline_py"),
        "previous_requirements": payload.get("requirements"),
        "repair_functions": sorted(repair_functions.values()),
        "repair_flow_ids": sorted(repair_functions),
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
    }
    return await _ask_pipeline(
        config, prompt, usage_callback, reply_format, code_key="functions_py"
    )
