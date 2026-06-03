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
}

DEFAULT_LLM_MODEL_FAMILY = "unknown"
DEV_SERVER_CONFIG_ID = "dev-env"

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


def is_dev_llm_config_enabled() -> bool:
    return _bool_config(os.getenv("INLUMEN_DEV_LLM_CONFIG_ENABLED"), False)


def _env_llm_config() -> dict[str, Any]:
    return {
        "provider": os.getenv("LLM_PROVIDER", "").strip(),
        "model": os.getenv("LLM_MODEL", "").strip(),
        "base_url": os.getenv("LLM_BASE_URL", "").strip(),
        "api_key": os.getenv("LLM_API_KEY", "").strip(),
        "model_family": os.getenv("LLM_MODEL_FAMILY", "").strip(),
        "max_tokens": os.getenv("LLM_MAX_TOKENS", "").strip(),
        "openrouter_provider_only": os.getenv("LLM_OPENROUTER_PROVIDER_ONLY", "").strip(),
    }


def server_managed_llm_config_metadata() -> dict[str, Any] | None:
    if not is_dev_llm_config_enabled():
        return None

    env_config = _env_llm_config()
    if not all(env_config.get(key) for key in ("provider", "model", "base_url", "api_key")):
        return None

    provider = _normalize_provider(str(env_config["provider"]))
    model = str(env_config["model"])
    if provider == "openrouter":
        model = OPENROUTER_MODEL_ALIASES.get(model.lower(), model)

    return {
        "id": DEV_SERVER_CONFIG_ID,
        "name": os.getenv("LLM_CONFIG_NAME", "").strip() or "Development LLM (.env)",
        "provider": provider,
        "model": model,
        "baseUrl": str(env_config["base_url"]),
        "base_url": str(env_config["base_url"]),
        "serverManagedKey": True,
        "server_managed_key": True,
        "readOnly": True,
        "read_only": True,
    }


def resolve_llm_config(raw_config: Optional[Mapping[str, Any]] = None) -> LLMConfig:
    raw = raw_config or {}
    server_managed_requested = _bool_config(
        _raw_config_value(raw, "server_managed_key", "serverManagedKey"),
        False,
    )
    use_env_config = is_dev_llm_config_enabled() and (not raw or server_managed_requested)
    env_config = _env_llm_config() if use_env_config else {}
    config_source = env_config if use_env_config else raw
    provider = _normalize_provider(
        config_source.get("provider")
        or config_source.get("llm_provider")
        or config_source.get("providerName")
    )
    model = str(config_source.get("model") or "").strip()
    if provider == "openrouter":
        model = OPENROUTER_MODEL_ALIASES.get(model.lower(), model)
    base_url = str(config_source.get("base_url") or config_source.get("baseUrl") or "").strip()
    api_key = str(config_source.get("api_key") or config_source.get("apiKey") or "").strip()
    model_family = (
        str(config_source.get("model_family") or config_source.get("modelFamily") or "").strip()
        or DEFAULT_LLM_MODEL_FAMILY
    )
    max_tokens = _positive_int_config(
        _raw_config_value(config_source, "max_tokens", "maxTokens"),
        None,
    )
    openrouter_provider_only = _string_tuple_config(
        _raw_config_value(
            config_source,
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
        raise ValueError("LLM base URL is required. Configure it in the UI LLM settings.")
    if not api_key:
        raise ValueError(
            f"LLM API key is required for provider '{provider}'. "
            "Enter it in the UI LLM settings for this browser session."
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
