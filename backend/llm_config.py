from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

from autogen_core.models import ChatCompletionClient
from application_llm import application_llm_request_config
from inlumen.llm_requests import request_policy
from inlumen.llm_autogen import OpenAIResponsesClient, ProviderAwareChatClient as OpenAIChatCompletionClient

# OpenAI-compatible LLM providers. The app no longer starts or depends on a
# local Ollama daemon; cloud and on-prem endpoints are selected by base URL.
LLM_PROVIDER_PRESETS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
    },
    "ollama_cloud": {
        "base_url": "https://ollama.com/v1",
    },
    "custom": {
        "base_url": "",
    },
}

DEFAULT_LLM_MODEL_FAMILY = "unknown"

OPENROUTER_MODEL_ALIASES = {
    "gpt-4o": "openai/gpt-4o",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "gpt-oss:120b": "openai/gpt-oss-120b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
    "gpt-oss:20b": "openai/gpt-oss-20b",
}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    model_family: str = "unknown"
    max_tokens: Optional[int] = None
    api_format: str = "auto"
    reasoning_effort: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    openrouter_provider_only: tuple[str, ...] = ()
    supports_vision: bool = False
    supports_function_calling: bool = True
    supports_json_output: bool = True
    supports_structured_output: bool = True


def _normalize_provider(provider: str | None) -> str:
    normalized = (provider or "").strip().lower().replace("-", "_")
    aliases = {
        "open_router": "openrouter",
        "ollama": "ollama_cloud",
        "ollama_cloud": "ollama_cloud",
        "on_premise": "custom",
        "on_prem": "custom",
        "self_hosted": "custom",
    }
    normalized = aliases.get(normalized, normalized)
    if not normalized:
        raise ValueError("LLM provider is required.")
    if normalized not in LLM_PROVIDER_PRESETS:
        return "custom"
    return normalized


def _bool_config(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _positive_int_config(value: Any, default: Optional[int]) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _string_tuple_config(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip().lower() for item in value if str(item).strip())
    return tuple(item.strip().lower() for item in str(value).split(",") if item.strip())


def _raw_config_value(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw.get(key) is not None:
            return raw.get(key)
    return None


def resolve_llm_config(raw_config: Optional[Mapping[str, Any]] = None) -> LLMConfig:
    raw = application_llm_request_config(raw_config or {})
    provider = _normalize_provider(
        raw.get("provider") or raw.get("llm_provider") or raw.get("providerName")
    )
    model = str(raw.get("model") or "").strip()
    if provider == "openrouter":
        model = OPENROUTER_MODEL_ALIASES.get(model.lower(), model)
    base_url = str(raw.get("base_url") or raw.get("baseUrl") or "").strip()
    api_key = str(raw.get("api_key") or raw.get("apiKey") or "").strip()
    if not api_key:
        # Import lazily: this module is also used by non-request tooling, while
        # credential resolution is meaningful only inside the gateway request.
        from llm_credential_store import get_llm_credential

        api_key = (
            get_llm_credential(
                str(raw.get("credential_id") or raw.get("config_id") or "")
            )
            or ""
        )
    model_family = (
        str(raw.get("model_family") or raw.get("modelFamily") or "").strip()
        or DEFAULT_LLM_MODEL_FAMILY
    )
    max_tokens = _positive_int_config(
        _raw_config_value(
            raw,
            "max_completion_tokens",
            "maxCompletionTokens",
            "max_output_tokens",
            "maxOutputTokens",
            "max_tokens",
            "maxTokens",
        ),
        None,
    )
    openrouter_provider_only = _string_tuple_config(
        _raw_config_value(
            raw,
            "openrouter_provider_only",
            "openRouterProviderOnly",
            "openrouterProviderOnly",
            "openrouter_provider",
            "openRouterProvider",
        )
    )

    if not model:
        raise ValueError("LLM model is required.")
    if not base_url:
        raise ValueError(
            "LLM base URL is required. Configure it in the UI LLM settings."
        )
    if not api_key:
        raise ValueError(
            f"LLM API key is required for provider '{provider}'. "
            "Save it in the UI LLM settings before using this configuration."
        )

    api_format = str(_raw_config_value(raw, "api_format", "apiFormat") or "auto")
    reasoning_effort = _raw_config_value(raw, "reasoning_effort", "reasoningEffort")
    request_policy(
        model,
        base_url,
        provider=provider,
        api_format=api_format,
        reasoning_effort=reasoning_effort,
        tools=True,
    )
    return LLMConfig(
        api_format=api_format,
        reasoning_effort=reasoning_effort,
        temperature=raw.get("temperature"),
        top_p=raw.get("top_p"),
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        model_family=model_family,
        max_tokens=max_tokens,
        openrouter_provider_only=openrouter_provider_only,
        supports_vision=_bool_config(
            _raw_config_value(raw, "supports_vision", "supportsVision"), False
        ),
        supports_function_calling=_bool_config(
            _raw_config_value(
                raw, "supports_function_calling", "supportsFunctionCalling"
            ),
            True,
        ),
        supports_json_output=_bool_config(
            _raw_config_value(raw, "supports_json_output", "supportsJsonOutput"), True
        ),
        supports_structured_output=_bool_config(
            _raw_config_value(
                raw, "supports_structured_output", "supportsStructuredOutput"
            ),
            True,
        ),
    )


def llm_config_from_payload(payload: Mapping[str, Any]) -> LLMConfig:
    raw_config = payload.get("llm_config")
    if isinstance(raw_config, Mapping):
        return resolve_llm_config(raw_config)
    return resolve_llm_config(payload)


def model_info(llm_config: LLMConfig) -> dict:
    return {
        "vision": llm_config.supports_vision,
        "function_calling": llm_config.supports_function_calling,
        "json_output": llm_config.supports_json_output,
        "family": llm_config.model_family,
        "structured_output": llm_config.supports_structured_output,
    }


def select_model_client(
    llm_config: Optional[LLMConfig] = None,
    *,
    parallel_tool_calls: Optional[bool] = None,
) -> ChatCompletionClient:
    resolved_config = llm_config or resolve_llm_config()
    kwargs: dict[str, Any] = {
        "model": resolved_config.model,
        "api_key": resolved_config.api_key,
        "base_url": resolved_config.base_url,
        "model_info": model_info(resolved_config),
    }
    policy = request_policy(
        resolved_config.model,
        resolved_config.base_url,
        provider=resolved_config.provider,
        api_format=resolved_config.api_format,
        reasoning_effort=resolved_config.reasoning_effort,
        tools=True,
    )
    # AutoGen accepts chat-shaped constructor arguments; the Responses adapter
    # translates them at the request boundary.
    chat_policy = replace(policy, api_format="chat_completions")
    kwargs.update(
        chat_policy.parameters(
            resolved_config.max_tokens,
            temperature=resolved_config.temperature,
            top_p=resolved_config.top_p,
        )
    )
    if parallel_tool_calls is not None:
        kwargs["parallel_tool_calls"] = parallel_tool_calls
    if resolved_config.provider == "openrouter":
        default_headers = {
            "HTTP-Referer": "https://github.com/DATAPACT/inLUMEN",
            "X-OpenRouter-Title": "inLUMEN",
        }
        kwargs["default_headers"] = default_headers
        if resolved_config.openrouter_provider_only:
            kwargs["extra_body"] = {
                "provider": {
                    "only": list(resolved_config.openrouter_provider_only),
                },
            }
    if policy.api_format == "responses":
        return OpenAIResponsesClient(request_policy=policy, **kwargs)
    return OpenAIChatCompletionClient(**kwargs)


def log_llm_selection(prefix: str, llm_config: LLMConfig) -> None:
    openrouter_route = (
        f", openrouter_provider_only={','.join(llm_config.openrouter_provider_only)}"
        if llm_config.openrouter_provider_only
        else ""
    )
    print(
        f"[llm_config.py] {prefix}: provider={llm_config.provider}, "
        f"model={llm_config.model}, base_url={llm_config.base_url}"
        f"{f', max_tokens={llm_config.max_tokens}' if llm_config.max_tokens is not None else ''}"
        f"{openrouter_route}"
    )
