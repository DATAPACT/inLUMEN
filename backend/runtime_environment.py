"""Static discovery of runtime environment variables used by Python Tasks."""

from __future__ import annotations

import ast
import re
from typing import Any


RESERVED_RUNTIME_ENVIRONMENT = frozenset(
    {
        "PIPELINE_INPUT_DIR",
        "PIPELINE_OUTPUT_DIR",
        "PIPELINE_PARAMS_JSON",
        "INLUMEN_FLOW_ID",
        "INLUMEN_STEP_LABEL",
        "INLUMEN_STEP_TYPE",
        "INLUMEN_PARAMS_JSON",
    }
)
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:api_?key|token|secret|password|passwd|credential|private_?key)(?:_|$)",
    re.IGNORECASE,
)


def _literal_environment_name(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = node.value.strip()
        if _ENVIRONMENT_NAME_RE.fullmatch(name):
            return name
    return ""


def discover_runtime_environment(source: str) -> list[dict[str, Any]]:
    """Return deterministic env requirements inferred from Python source.

    Indexing ``os.environ`` is treated as required. ``getenv``/``environ.get``
    are optional because their absence is part of the API's normal behavior.
    Explicit node-manifest declarations can be merged by the caller later.
    """
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, TypeError, ValueError):
        return []

    discovered: dict[str, bool] = {}
    for node in ast.walk(tree):
        name = ""
        required = False
        if isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
                and value.attr == "environ"
            ):
                name = _literal_environment_name(node.slice)
                required = True
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            is_getenv = (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "os"
                and function.attr == "getenv"
            ) or (isinstance(function, ast.Name) and function.id == "getenv")
            is_environ_get = (
                isinstance(function, ast.Attribute)
                and function.attr == "get"
                and isinstance(function.value, ast.Attribute)
                and isinstance(function.value.value, ast.Name)
                and function.value.value.id == "os"
                and function.value.attr == "environ"
            )
            if is_getenv or is_environ_get:
                name = _literal_environment_name(node.args[0])
        if name and name not in RESERVED_RUNTIME_ENVIRONMENT:
            discovered[name] = discovered.get(name, False) or required

    return [
        {
            "name": name,
            "required": discovered[name],
            "secret": bool(_SECRET_NAME_RE.search(name)),
            "source": "static-python-analysis",
        }
        for name in sorted(discovered)
    ]


def merge_runtime_environment(
    discovered: list[dict[str, Any]],
    declared: object,
) -> list[dict[str, Any]]:
    """Merge explicit manifest declarations over advisory static discovery."""
    merged = {
        str(entry.get("name") or "").strip(): dict(entry)
        for entry in discovered
        if isinstance(entry, dict) and str(entry.get("name") or "").strip()
    }
    if isinstance(declared, list):
        for entry in declared:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not _ENVIRONMENT_NAME_RE.fullmatch(name) or name in RESERVED_RUNTIME_ENVIRONMENT:
                continue
            merged[name] = {
                **merged.get(name, {}),
                **entry,
                "name": name,
                "source": "node-manifest",
            }
    return [merged[name] for name in sorted(merged)]
