from __future__ import annotations

import json
import re
from typing import Any


SENSITIVE_PARAMETER_RE = re.compile(
    r"(^|[_\-.])(api[_\-.]?key|access[_\-.]?key|client[_\-.]?secret|private[_\-.]?key|"
    r"password|passphrase|secret|token|credential|authorization)($|[_\-.])",
    re.IGNORECASE,
)


def is_sensitive_parameter_name(value: Any) -> bool:
    name = str(value or "").strip()
    compact = re.sub(r"[^a-z0-9]", "", name.lower())
    return bool(SENSITIVE_PARAMETER_RE.search(name)) or compact in {
        "apikey",
        "accesskey",
        "clientsecret",
        "privatekey",
        "password",
        "passphrase",
        "secret",
        "token",
        "credential",
        "authorization",
    }


def normalize_secret_param_keys(value: Any, parameters: Any) -> list[str]:
    param_keys = {
        str(key)
        for key in (parameters.keys() if isinstance(parameters, dict) else [])
    }
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = None

    if isinstance(value, list):
        candidates = [str(item).strip() for item in value]
    else:
        candidates = [key for key in param_keys if is_sensitive_parameter_name(key)]

    result: list[str] = []
    seen: set[str] = set()
    for key in candidates:
        if not key or key not in param_keys or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def secret_params_json(value: Any, parameters: Any) -> str:
    return json.dumps(
        normalize_secret_param_keys(value, parameters),
        ensure_ascii=False,
    )
