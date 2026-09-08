"""Deployment-owned LLM access, independent of workspace credential storage.

Requests select a reserved config ID; all connection settings are rebuilt from
the configuration saved by an application administrator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from flask import g, has_request_context


APPLICATION_LLM_ID = "application-llm"
_PROVIDER_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama_cloud": "https://ollama.com/v1",
}


@dataclass(frozen=True)
class ApplicationLLM:
    provider: str
    base_url: str
    model: str
    codegen_model: str
    api_key: str = field(repr=False)
    max_tokens: int = 8192
    provider_only: tuple[str, ...] = ()
    codegen_provider_only: tuple[str, ...] = ()

    def public_config(self) -> dict[str, Any]:
        return {
            "id": APPLICATION_LLM_ID,
            "name": "Application-provided LLM",
            "provider": self.provider,
            "baseUrl": self.base_url,
            "base_url": self.base_url,
            "model": self.model,
            "codegenModel": self.codegen_model,
            "codegen_model": self.codegen_model,
            "openrouterProviderOnly": list(self.provider_only),
            "codegenOpenrouterProviderOnly": list(self.codegen_provider_only),
            "has_api_key": True,
            "readOnly": True,
            "read_only": True,
            "applicationProvided": True,
        }


def validate_application_llm_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    base_url = str(raw.get("baseUrl") or raw.get("base_url") or _PROVIDER_URLS["openrouter"]).strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        valid_url = (
            parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
            and not any(char.isspace() for char in base_url)
        )
        parsed.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("Provider URL must use HTTPS without credentials, query parameters, or a fragment.")
    provider = next((name for name, url in _PROVIDER_URLS.items() if base_url == url), "custom")
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ValueError("A model is required.")
    codegen_model = str(raw.get("codegenModel") or raw.get("codegen_model") or model).strip()

    def routes(camel: str, snake: str) -> list[str]:
        value = raw.get(camel, raw.get(snake, []))
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("Provider routing must be a list of names.")
        return list(dict.fromkeys(item.strip().lower() for item in value if item.strip())) if provider == "openrouter" else []

    return {
        "provider": provider, "base_url": base_url, "model": model,
        "codegen_model": codegen_model,
        "provider_only": routes("openrouterProviderOnly", "openrouter_provider_only"),
        "codegen_provider_only": routes("codegenOpenrouterProviderOnly", "codegen_openrouter_provider_only"),
    }


def application_llm_settings() -> dict:
    """Metadata only, including disabled settings for the administrator."""
    from application_llm_store import read_application_llm
    saved = read_application_llm()
    config = saved["config"]
    return {
        "config": ApplicationLLM(**config, api_key="").public_config() if config else None,
        "enabled": saved["enabled"], "revision": saved["revision"],
    }


def load_application_llm() -> ApplicationLLM | None:
    from application_llm_store import read_application_llm
    saved = read_application_llm(include_key=True)
    if not saved["enabled"] or not saved["config"]:
        return None
    return ApplicationLLM(**saved["config"], api_key=saved["api_key"])


def application_llm_request_config(
    raw: Mapping[str, Any], *, purpose: str = "chat",
) -> Mapping[str, Any]:
    if not any(str(raw.get(key) or "").strip() == APPLICATION_LLM_ID for key in ("credential_id", "config_id")):
        return raw
    # Do not use current_principal(): it falls back to a local identity when no
    # authenticated principal has been established. require_auth sets g first.
    if not has_request_context() or getattr(g, "inlumen_principal", None) is None:
        raise ValueError("Application LLM access requires an authorized workspace request.")
    from auth_middleware import is_auth_enabled
    if os.getenv("APP_ENV", "development").strip().lower() == "production" and not is_auth_enabled():
        raise ValueError("Application LLM access requires authentication in production.")
    configured = load_application_llm()
    if configured is None:
        raise ValueError("Application-provided LLM is disabled.")
    codegen = purpose == "codegen"
    # Intentionally replace the whole mapping, including aliases, routing,
    # capabilities and token limits. Never attach this key to client settings.
    return {
        "provider": configured.provider,
        "base_url": configured.base_url,
        "model": configured.codegen_model if codegen else configured.model,
        "api_key": configured.api_key,
        "max_output_tokens" if codegen else "max_tokens": configured.max_tokens,
        "model_family": "code" if codegen else "unknown",
        "supports_function_calling": True,
        "supports_json_output": True,
        "supports_structured_output": True,
        "supports_vision": False,
        "openrouter_provider_only": list(configured.codegen_provider_only if codegen else configured.provider_only),
        **({"timeout_seconds": 180} if codegen else {}),
    }
