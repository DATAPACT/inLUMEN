"""Validation and normalization of ``inlumen.plugin@1`` manifests.

A plugin describes a third-party tool that is already deployed behind a REST
API. The manifest carries the endpoint, the operations inLUMEN may call, the
parameters users configure, the secrets the tool needs (declared only, never
their values) and instructions for the Pipeline Designer agents.
"""
from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "inlumen.plugin@1"
PLUGIN_TEMPLATE_PREFIX = "plugin:"
SCHEMA_PATH = Path(__file__).with_name("plugin-manifest.schema.json")

# Step parameters owned by the plugin adapter itself.
RESERVED_PARAMETER_NAMES = frozenset({
    "operation",
    "base_url",
    "model_plan",
    "output_artifact",
    "output_filename",
    "output_mode",
    "timeout",
})

# Values of the output_mode step parameter, for operations with response.merge.
OUTPUT_MODE_ANNOTATIONS = "annotations"
OUTPUT_MODE_ENRICH = "input_with_annotations"
OUTPUT_MODES = (OUTPUT_MODE_ANNOTATIONS, OUTPUT_MODE_ENRICH)
DEFAULT_MERGE_FILENAME = "annotated.json"

_PATH_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DEFAULT_PORT_NAMES = {
    "source": {"outputs": "data"},
    "task": {"inputs": "input", "outputs": "output"},
    "destination": {"inputs": "data"},
}


class PluginManifestError(ValueError):
    """Raised when a plugin manifest is invalid; ``errors`` lists every problem."""

    def __init__(self, message: str, errors: list[str] | None = None):
        self.errors = list(errors or [message])
        super().__init__(message if not errors else f"{message}: {'; '.join(self.errors)}")


@lru_cache(maxsize=1)
def manifest_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def plugin_template(plugin_id: str) -> str:
    """Template label stored on a STEP that is backed by a plugin."""
    return f"{PLUGIN_TEMPLATE_PREFIX}{plugin_id}"


def plugin_id_from_template(template: object) -> str | None:
    """Return the plugin id for ``plugin:<id>`` templates, else ``None``."""
    value = " ".join(str(template or "").strip().split())
    if not value.lower().startswith(PLUGIN_TEMPLATE_PREFIX):
        return None
    plugin_id = value[len(PLUGIN_TEMPLATE_PREFIX):].strip().lower()
    return plugin_id or None


def is_plugin_template(template: object) -> bool:
    return plugin_id_from_template(template) is not None


def _schema_errors(data: Any) -> list[str]:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(manifest_schema())
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda item: list(item.path)):
        location = "/".join(str(part) for part in error.path) or "<root>"
        errors.append(f"{location}: {error.message}")
    return errors


def _default_body(step_type: str, method: str) -> str:
    if method == "GET" or method == "DELETE":
        return "none"
    if step_type == "source":
        return "parameters"
    return "input"


def validate_plugin_manifest(data: Any) -> dict[str, Any]:
    """Validate ``data`` and return a normalized deep copy.

    Raises :class:`PluginManifestError` listing every schema and semantic
    problem so whoever writes a plugin definition gets actionable feedback at once.
    """
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise PluginManifestError("Plugin manifest is not valid JSON") from exc
    errors = _schema_errors(data)
    if errors:
        raise PluginManifestError("Invalid plugin manifest", errors)

    manifest = copy.deepcopy(data)
    step_type = manifest["step_type"]
    errors = []

    # Operations ---------------------------------------------------------
    operation_ids: set[str] = set()
    for operation in manifest["operations"]:
        if operation["id"] in operation_ids:
            errors.append(f"operations: duplicate operation id {operation['id']!r}")
        operation_ids.add(operation["id"])
        request = operation.setdefault("request", {})
        request.setdefault("body", _default_body(step_type, operation["method"]))
        if request["body"] in {"input", "multipart"} and step_type == "source":
            errors.append(
                f"operations/{operation['id']}: a source has no inputs, so request.body "
                f"cannot be {request['body']!r}"
            )
        if request["body"] != "none" and operation["method"] == "GET":
            errors.append(f"operations/{operation['id']}: GET requests cannot send a body")
        if request["body"] == "multipart":
            request.setdefault("file_field", "file")
        if request.get("input_field") and request["body"] not in {"form", "parameters"}:
            errors.append(
                f"operations/{operation['id']}: request.input_field requires body 'form' or 'parameters'"
            )
        if request.get("input_field") and step_type == "source":
            errors.append(f"operations/{operation['id']}: a source has no inputs to send in input_field")
        response = operation.setdefault("response", {})
        response.setdefault("format", "json")
        response.setdefault("representation", "file")
        async_spec = operation.get("async")
        if async_spec:
            async_spec.setdefault("job_id_field", "job_id")
            async_spec.setdefault("status_field", "status")
            async_spec.setdefault("done_values", ["completed"])
            async_spec.setdefault("failed_values", ["failed"])
            async_spec.setdefault("error_field", "error")
            async_spec.setdefault("interval_seconds", 2)
            async_spec.setdefault("timeout_seconds", 600)
            if response.get("format") != "json":
                errors.append(f"operations/{operation['id']}: asynchronous operations return JSON results")
        if response["representation"] == "directory" and step_type != "task":
            errors.append(
                f"operations/{operation['id']}: only task plugins can return a directory"
            )
        default_filename = (
            "results" if response["representation"] == "directory"
            else f"response.{response['format']}"
        )
        response.setdefault("filename", default_filename)
        merge = response.get("merge")
        if merge:
            merge.setdefault("filename", DEFAULT_MERGE_FILENAME)
            if response["format"] != "json":
                errors.append(f"operations/{operation['id']}: response.merge requires a JSON response")
            if not request.get("input_field"):
                errors.append(
                    f"operations/{operation['id']}: response.merge requires request.input_field "
                    "(the input text the response annotates)"
                )

    # Parameters -----------------------------------------------------------
    parameter_names: set[str] = set()
    for parameter in manifest.setdefault("parameters", []):
        name = parameter["name"]
        if name in parameter_names:
            errors.append(f"parameters: duplicate parameter {name!r}")
        if name in RESERVED_PARAMETER_NAMES:
            errors.append(f"parameters: {name!r} is reserved by the plugin adapter")
        parameter_names.add(name)
        parameter.setdefault("type", "string")
        parameter.setdefault("required", False)
        parameter.setdefault("location", "query")
        if "." in parameter.get("api_name", "") and parameter["location"] != "body":
            errors.append(f"parameters/{name}: a dotted api_name is only valid for body parameters")
        unknown = set(parameter.get("operations") or []) - operation_ids
        if unknown:
            errors.append(f"parameters/{name}: unknown operations {sorted(unknown)}")
        if "enum" in parameter and "default" in parameter and parameter["default"] not in parameter["enum"]:
            errors.append(f"parameters/{name}: default is not one of enum")

    for operation in manifest["operations"]:
        placeholders = set(_PATH_PLACEHOLDER_RE.findall(operation["path"]))
        for placeholder in placeholders:
            match = next(
                (p for p in manifest["parameters"] if p["name"] == placeholder),
                None,
            )
            if match is None or match["location"] != "path":
                errors.append(
                    f"operations/{operation['id']}: path placeholder {{{placeholder}}} "
                    "must be declared as a parameter with location 'path'"
                )
        if operation["request"]["body"] in {"input", "none"}:
            body_parameters = [
                p["name"] for p in parameters_for_operation(manifest, operation["id"])
                if p["location"] == "body"
            ]
            if body_parameters:
                errors.append(
                    f"operations/{operation['id']}: body parameters {body_parameters} "
                    f"cannot be sent with request.body {operation['request']['body']!r}"
                )

    # Secrets and authentication ------------------------------------------
    secret_names: set[str] = set()
    for secret in manifest.setdefault("secrets", []):
        if secret["name"] in secret_names:
            errors.append(f"secrets: duplicate secret {secret['name']!r}")
        if secret["name"] in parameter_names or secret["name"] in RESERVED_PARAMETER_NAMES:
            errors.append(f"secrets: {secret['name']!r} collides with a parameter name")
        secret_names.add(secret["name"])
        secret.setdefault("required", True)
        secret.setdefault("label", secret["name"].replace("_", " ").capitalize())

    auth = manifest.setdefault("auth", {"type": "none"})
    if auth["type"] != "none":
        if not auth.get("secret"):
            errors.append("auth: secret is required when auth.type is not 'none'")
        elif auth["secret"] not in secret_names:
            errors.append(f"auth: secret {auth['secret']!r} is not declared in secrets")
        placements = [key for key in ("header", "query_parameter", "form_field") if auth.get(key)]
        if len(placements) > 1:
            errors.append(f"auth: set only one of header, query_parameter or form_field (got {placements})")
        if auth.get("form_field") and any(
            op["request"]["body"] != "form" for op in manifest["operations"]
        ):
            errors.append("auth: form_field requires every operation to use body 'form'")
        if auth["type"] == "api_key" and not placements:
            auth.setdefault("header", "X-API-Key")
        if auth["type"] == "bearer":
            auth.setdefault("header", "Authorization")
            auth.setdefault("prefix", "Bearer")

    # Ports ----------------------------------------------------------------
    ports = manifest.setdefault("ports", {})
    defaults = _DEFAULT_PORT_NAMES[step_type]
    for direction in ("inputs", "outputs"):
        if direction not in defaults:
            if ports.get(direction):
                errors.append(f"ports: a {step_type} plugin cannot declare {direction}")
            ports[direction] = []
            continue
        if not ports.get(direction):
            ports[direction] = [{"name": defaults[direction]}]
        for port in ports[direction]:
            port.setdefault("type", "any")
            port.setdefault("description", "")

    manifest.setdefault("publisher", "")
    manifest.setdefault("instructions", "")
    manifest.setdefault("tags", [])
    if errors:
        raise PluginManifestError("Invalid plugin manifest", errors)
    return manifest


def supports_enrich(operation: dict[str, Any]) -> bool:
    """Whether the operation can publish its input with the response added."""
    return bool((operation.get("response") or {}).get("merge"))


def parameters_for_operation(manifest: dict[str, Any], operation_id: str) -> list[dict[str, Any]]:
    return [
        parameter
        for parameter in manifest.get("parameters") or []
        if not parameter.get("operations") or operation_id in parameter["operations"]
    ]


def default_operation(manifest: dict[str, Any]) -> dict[str, Any]:
    return manifest["operations"][0]


def resolve_operation(manifest: dict[str, Any], operation_id: object = None) -> dict[str, Any]:
    wanted = str(operation_id or "").strip()
    if not wanted:
        return default_operation(manifest)
    for operation in manifest["operations"]:
        if operation["id"] == wanted:
            return operation
    available = ", ".join(op["id"] for op in manifest["operations"])
    raise PluginManifestError(
        f"Plugin {manifest['id']!r} has no operation {wanted!r}. Available: {available}."
    )


def plugin_ports(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Ports in the shape used by STEP.ports_json."""
    def convert(port: dict[str, Any]) -> dict[str, Any]:
        port_id = re.sub(r"[^a-z0-9_.-]+", "-", port["name"].lower()).strip("-") or "port"
        return {
            "id": port_id,
            "name": port["name"],
            "type": port.get("type") or "any",
            "required": True,
            "description": port.get("description") or "",
        }

    ports = manifest.get("ports") or {}
    return {
        "inputs": [convert(port) for port in ports.get("inputs") or []],
        "outputs": [convert(port) for port in ports.get("outputs") or []],
    }


def required_parameter_names(manifest: dict[str, Any], operation_id: object = None) -> list[str]:
    """Non-secret parameters the user must set for the chosen operation."""
    operation = resolve_operation(manifest, operation_id)
    return [
        parameter["name"]
        for parameter in parameters_for_operation(manifest, operation["id"])
        if parameter.get("required") and "default" not in parameter
    ]


def secret_parameter_names(manifest: dict[str, Any]) -> list[str]:
    return [secret["name"] for secret in manifest.get("secrets") or []]


def default_parameters(manifest: dict[str, Any]) -> dict[str, Any]:
    """Initial STEP.param for a newly configured plugin step."""
    values: dict[str, Any] = {"operation": default_operation(manifest)["id"]}
    if any(supports_enrich(operation) for operation in manifest["operations"]):
        values["output_mode"] = OUTPUT_MODE_ANNOTATIONS
    for parameter in manifest.get("parameters") or []:
        values[parameter["name"]] = parameter.get("default", "")
    # Only required secrets start on the step: an unconfigured optional secret
    # would otherwise fail runtimes that inject every sensitive key.
    for secret in manifest.get("secrets") or []:
        if secret.get("required", True):
            values[secret["name"]] = ""
    return values


def plugin_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Compact description used by the agent and plugin listings."""
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "name": manifest["name"],
        "description": manifest["description"],
        "publisher": manifest.get("publisher", ""),
        "step_type": manifest["step_type"],
        "template": plugin_template(manifest["id"]),
        "operations": [
            {"id": op["id"], "label": op.get("label") or op["id"], "description": op.get("description", "")}
            for op in manifest["operations"]
        ],
        "tags": manifest.get("tags", []),
    }
