import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from autogen_core.models import ChatCompletionClient
from autogen_ext.models.openai import OpenAIChatCompletionClient

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
    # Kept for backward compatibility with existing OPENAI_* deployments.
    "openai": {
        "base_url": "https://api.openai.com/v1",
    },
}

DEFAULT_LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter").strip() or "openrouter"
DEFAULT_CHAT_MODEL = os.getenv("LLM_MODEL", "").strip() or "gpt-oss-120b"
DEFAULT_LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
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
    api_key: str
    model_family: str = "unknown"
    max_tokens: Optional[int] = None
    openrouter_provider_only: tuple[str, ...] = ()
    supports_vision: bool = False
    supports_function_calling: bool = True
    supports_json_output: bool = True
    supports_structured_output: bool = True


def _normalize_provider(provider: str | None) -> str:
    normalized = (provider or DEFAULT_LLM_PROVIDER).strip().lower().replace("-", "_")
    aliases = {
        "open_router": "openrouter",
        "ollama": "ollama_cloud",
        "ollama_cloud": "ollama_cloud",
        "on_premise": "custom",
        "on_prem": "custom",
        "self_hosted": "custom",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in LLM_PROVIDER_PRESETS:
        return "custom"
    return normalized


def _api_key_from_env() -> str:
    return os.getenv("LLM_API_KEY", "").strip()


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
    raw = raw_config or {}
    provider = _normalize_provider(
        raw.get("provider")
        or raw.get("llm_provider")
        or raw.get("providerName")
    )
    preset = LLM_PROVIDER_PRESETS[provider]
    model = str(raw.get("model") or "").strip() or DEFAULT_CHAT_MODEL
    if provider == "openrouter" and model.lower() in {"llama3.1", "llama3.1:8b"}:
        model = DEFAULT_CHAT_MODEL
    if provider == "openrouter":
        model = OPENROUTER_MODEL_ALIASES.get(model.lower(), model)
    base_url = (
        str(raw.get("base_url") or raw.get("baseUrl") or "").strip()
        or DEFAULT_LLM_BASE_URL
        or preset["base_url"]
    )
    api_key = (
        str(raw.get("api_key") or raw.get("apiKey") or "").strip()
        or _api_key_from_env()
    )
    model_family = (
        str(raw.get("model_family") or raw.get("modelFamily") or "").strip()
        or DEFAULT_LLM_MODEL_FAMILY
    )
    max_tokens = _positive_int_config(
        _raw_config_value(raw, "max_tokens", "maxTokens"),
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
        raise ValueError("LLM base URL is required for OpenAI-compatible endpoints.")
    if not api_key:
        raise ValueError(
            f"LLM API key is required for provider '{provider}'. "
            "Provide it in the configuration or set LLM_API_KEY."
        )

    return LLMConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        model_family=model_family,
        max_tokens=max_tokens,
        openrouter_provider_only=openrouter_provider_only,
        supports_vision=_bool_config(_raw_config_value(raw, "supports_vision", "supportsVision"), False),
        supports_function_calling=_bool_config(
            _raw_config_value(raw, "supports_function_calling", "supportsFunctionCalling"),
            True,
        ),
        supports_json_output=_bool_config(_raw_config_value(raw, "supports_json_output", "supportsJsonOutput"), True),
        supports_structured_output=_bool_config(
            _raw_config_value(raw, "supports_structured_output", "supportsStructuredOutput"),
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
    if resolved_config.max_tokens is not None:
        kwargs["max_tokens"] = resolved_config.max_tokens
    if parallel_tool_calls is not None:
        kwargs["parallel_tool_calls"] = parallel_tool_calls
    if resolved_config.provider == "openrouter":
        default_headers = {
            "HTTP-Referer": "http://localhost",
            "X-Title": "inLUMEN",
        }
        kwargs["default_headers"] = default_headers
        if resolved_config.openrouter_provider_only:
            kwargs["extra_body"] = {
                "provider": {
                    "only": list(resolved_config.openrouter_provider_only),
                },
            }
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
