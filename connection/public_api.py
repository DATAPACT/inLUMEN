from __future__ import annotations

import functools
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from flask import Blueprint, jsonify, make_response, request

from auth_middleware import is_auth_enabled, validate_keycloak_bearer_token
from async_runtime import run_async
from deployment_artifacts import (
    DeploymentArtifactValidationError,
    build_argo_workflow_yaml,
    extract_pipeline_steps,
)
from graph_client import fetch_pipeline_graph, fetch_pipeline_versions
from minio_gateway import get_minio_client


logging.basicConfig(level=logging.INFO, format="%(message)s")
LOGGER = logging.getLogger("inlumen.public_api")

API_VERSION = "1.0.0"
SIGNED_URL_EXPIRES_SECONDS = 3600
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def create_public_api_blueprint(neo4j_api_base_url: str) -> Blueprint:
    public_api = Blueprint("public_api", __name__)

    @public_api.after_request
    def log_public_api_response(response):
        _log_event(
            "public_api_request",
            method=request.method,
            path=request.path,
            status_code=response.status_code,
        )
        return response

    @public_api.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return _error_response(
            error.status_code,
            error.code,
            error.message,
            error.details,
        )

    @public_api.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        _log_event(
            "public_api_unhandled_error",
            path=request.path,
            error_type=type(error).__name__,
        )
        return _error_response(500, "internal_error", "Internal server error")

    @public_api.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"}), 200

    @public_api.route("/ready", methods=["GET"])
    def readiness():
        auth_checks = _auth_readiness_checks()
        if not _auth_checks_ready(auth_checks):
            return jsonify({
                "status": "not_ready",
                "checks": auth_checks,
            }), 503
        return jsonify({
            "status": "ready",
            "checks": auth_checks,
        }), 200

    @public_api.route("/docs", methods=["GET"])
    @public_api.route("/swagger", methods=["GET"])
    def swagger_ui():
        response = make_response(SWAGGER_UI_HTML, 200)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response

    @public_api.route("/openapi.json", methods=["GET", "OPTIONS"])
    @api_auth_required
    def openapi_json():
        if request.method == "OPTIONS":
            return _preflight_response()
        return jsonify(build_openapi_schema())

    @public_api.route("/api/v1/pipelines", methods=["GET", "OPTIONS"])
    @api_auth_required
    def list_pipelines():
        if request.method == "OPTIONS":
            return _preflight_response()
        graph = _load_pipeline_graph(neo4j_api_base_url)
        pipeline = _pipeline_summary_from_graph(graph)
        return jsonify({"pipelines": [pipeline] if pipeline else []}), 200

    @public_api.route("/api/v1/pipelines", methods=["POST", "OPTIONS"])
    @api_auth_required
    def create_pipeline():
        if request.method == "OPTIONS":
            return _preflight_response()
        payload = _validate_pipeline_create_request(_json_body())
        pipeline = _create_pipeline(neo4j_api_base_url, payload)
        return jsonify({"pipeline": pipeline}), 201

    @public_api.route("/api/v1/pipelines/<pipeline_id>", methods=["GET", "OPTIONS"])
    @api_auth_required
    def get_pipeline(pipeline_id: str):
        if request.method == "OPTIONS":
            return _preflight_response()
        requested_id = _validate_id("pipeline_id", pipeline_id)
        graph = _load_pipeline_graph(neo4j_api_base_url)
        pipeline = _pipeline_summary_from_graph(graph)
        if not pipeline or not _pipeline_id_matches(pipeline, requested_id):
            raise ApiError(404, "pipeline_not_found", "Pipeline not found")
        return jsonify({"pipeline": pipeline, "graph": graph}), 200

    @public_api.route("/api/v1/pipelines/<pipeline_id>/versions", methods=["GET", "OPTIONS"])
    @api_auth_required
    def list_pipeline_versions(pipeline_id: str):
        if request.method == "OPTIONS":
            return _preflight_response()
        requested_id = _validate_id("pipeline_id", pipeline_id)
        graph = _load_pipeline_graph(neo4j_api_base_url)
        pipeline = _pipeline_summary_from_graph(graph)
        if not pipeline or not _pipeline_id_matches(pipeline, requested_id):
            raise ApiError(404, "pipeline_not_found", "Pipeline not found")
        versions = _load_pipeline_versions(neo4j_api_base_url, include_graph=False)
        if not versions:
            versions = [_active_graph_version(graph, pipeline)]
        return jsonify({
            "pipeline_id": pipeline["id"],
            "versions": [_public_version(version) for version in versions],
        }), 200

    @public_api.route("/api/v1/pipelines/<pipeline_id>/artifacts/dockerfiles", methods=["GET", "OPTIONS"])
    @api_auth_required
    def get_pipeline_dockerfiles(pipeline_id: str):
        if request.method == "OPTIONS":
            return _preflight_response()
        graph, pipeline = _pipeline_graph_or_404(neo4j_api_base_url, pipeline_id)
        dockerfiles = _build_dockerfile_artifacts_or_error(graph)
        return jsonify({
            "pipeline_id": pipeline["id"],
            "version_id": pipeline["active_version_id"],
            "version_name": pipeline["active_version_name"],
            "dockerfiles": dockerfiles["dockerfiles"],
            "guardrails": dockerfiles["guardrails"],
        }), 200

    @public_api.route("/api/v1/pipelines/<pipeline_id>/artifacts/argo-workflow.yaml", methods=["GET", "OPTIONS"])
    @api_auth_required
    def get_pipeline_argo_workflow_yaml(pipeline_id: str):
        if request.method == "OPTIONS":
            return _preflight_response()
        graph, _pipeline = _pipeline_graph_or_404(neo4j_api_base_url, pipeline_id)
        yaml_text = _build_argo_workflow_yaml_or_error(graph)
        response = make_response(yaml_text, 200)
        response.headers["Content-Type"] = "application/x-yaml; charset=utf-8"
        return response

    @public_api.route(
        "/api/v1/pipelines/<pipeline_id>/versions/<version_id>/artifacts/dockerfiles",
        methods=["GET", "OPTIONS"],
    )
    @api_auth_required
    def get_pipeline_version_dockerfiles(pipeline_id: str, version_id: str):
        if request.method == "OPTIONS":
            return _preflight_response()
        graph, pipeline, version = _pipeline_version_graph_or_404(
            neo4j_api_base_url,
            pipeline_id,
            version_id,
        )
        dockerfiles = _build_dockerfile_artifacts_or_error(graph)
        public_version = _public_version(version)
        return jsonify({
            "pipeline_id": pipeline["id"],
            "version_id": public_version["id"],
            "version_name": public_version["version_name"],
            "dockerfiles": dockerfiles["dockerfiles"],
            "guardrails": dockerfiles["guardrails"],
        }), 200

    @public_api.route(
        "/api/v1/pipelines/<pipeline_id>/versions/<version_id>/artifacts/argo-workflow.yaml",
        methods=["GET", "OPTIONS"],
    )
    @api_auth_required
    def get_pipeline_version_argo_workflow_yaml(pipeline_id: str, version_id: str):
        if request.method == "OPTIONS":
            return _preflight_response()
        graph, _pipeline, _version = _pipeline_version_graph_or_404(
            neo4j_api_base_url,
            pipeline_id,
            version_id,
        )
        yaml_text = _build_argo_workflow_yaml_or_error(graph)
        response = make_response(yaml_text, 200)
        response.headers["Content-Type"] = "application/x-yaml; charset=utf-8"
        return response

    @public_api.route("/api/v1/workflows", methods=["GET", "OPTIONS"])
    @api_auth_required
    def list_workflows():
        if request.method == "OPTIONS":
            return _preflight_response()
        include_urls = _bool_query("include_download_urls", default=False)
        workflows = _list_workflows(neo4j_api_base_url, include_download_urls=include_urls)
        return jsonify({"workflows": workflows}), 200

    @public_api.route("/api/v1/workflows/versions", methods=["GET", "OPTIONS"])
    @api_auth_required
    def list_workflow_versions():
        if request.method == "OPTIONS":
            return _preflight_response()
        workflows = _list_workflows(neo4j_api_base_url, include_download_urls=False)
        return jsonify({
            "versions": [
                {
                    "workflow_id": workflow["id"],
                    "pipeline_id": workflow["pipeline_id"],
                    "version": workflow["version"],
                    "version_id": workflow["version_id"],
                    "version_name": workflow["version_name"],
                    "modified_at": workflow["modified_at"],
                }
                for workflow in workflows
            ]
        }), 200

    return public_api


def _configured_api_token() -> str:
    return os.getenv("API_AUTH_TOKEN", "").strip()


def _request_authorization_header() -> str | None:
    auth_header = request.headers.get("Authorization", "")
    return auth_header if auth_header else None


def _auth_readiness_checks() -> dict[str, str]:
    if is_auth_enabled():
        if os.getenv("KEYCLOAK_JWKS_URL", "").strip():
            return {"auth_mode": "keycloak", "keycloak_jwks_url": "configured"}
        return {"auth_mode": "keycloak", "keycloak_jwks_url": "missing"}
    if _configured_api_token():
        return {"auth_mode": "static_bearer", "api_auth_token": "configured"}
    return {"auth_mode": "static_bearer", "api_auth_token": "missing"}


def _auth_checks_ready(auth_checks: dict[str, str]) -> bool:
    if auth_checks["auth_mode"] == "keycloak":
        return auth_checks.get("keycloak_jwks_url") == "configured"
    return auth_checks.get("api_auth_token") == "configured"


def _keycloak_public_error_code(error_name: str, status_code: int) -> str:
    if status_code == 503:
        return "auth_unavailable"
    if status_code >= 500:
        return "auth_misconfigured"
    return "unauthorized" if error_name.lower() == "unauthorized" else "forbidden"


def api_auth_required(route_handler):
    @functools.wraps(route_handler)
    def decorated(*args, **kwargs):
        if request.method == "OPTIONS":
            return route_handler(*args, **kwargs)

        if is_auth_enabled():
            _claims, error = validate_keycloak_bearer_token()
            if error is not None:
                return _error_response(
                    error.status_code,
                    _keycloak_public_error_code(error.error, error.status_code),
                    error.detail,
                    error.details,
                )
            return route_handler(*args, **kwargs)

        configured_token = _configured_api_token()
        if not configured_token:
            _log_event("api_auth_misconfigured", path=request.path)
            return _error_response(
                500,
                "api_auth_not_configured",
                "API authentication is not configured",
            )

        auth_header = request.headers.get("Authorization", "")
        scheme, separator, provided_token = auth_header.partition(" ")
        if not separator or scheme.lower() != "bearer" or not provided_token.strip():
            return _error_response(401, "unauthorized", "Missing Bearer token")

        if not hmac.compare_digest(provided_token.strip(), configured_token):
            return _error_response(403, "forbidden", "Invalid Bearer token")

        return route_handler(*args, **kwargs)

    return decorated


def _preflight_response():
    return make_response("", 200)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
):
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return jsonify(payload), status_code


def _log_event(event: str, **fields: Any) -> None:
    safe_fields = {
        key: value
        for key, value in fields.items()
        if "token" not in key.lower() and "authorization" not in key.lower()
    }
    LOGGER.info(json.dumps({"event": event, **safe_fields}, sort_keys=True, default=str))


def _json_body() -> dict[str, Any]:
    if not request.is_json:
        raise ApiError(400, "invalid_json", "Expected application/json request body")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_json", "Expected a JSON object request body")
    return payload


def _validate_pipeline_create_request(payload: dict[str, Any]) -> dict[str, str]:
    allowed_fields = {"name", "label", "description", "version"}
    unexpected_fields = sorted(set(payload) - allowed_fields)
    if unexpected_fields:
        raise ApiError(
            422,
            "validation_error",
            "Unexpected request field",
            {"fields": unexpected_fields},
        )

    name = _clean_string(payload.get("name"), "name", required=True, max_length=120)
    label = _clean_string(payload.get("label"), "label", required=False, max_length=120) or name
    description = _clean_string(
        payload.get("description"),
        "description",
        required=False,
        max_length=1000,
    )
    version = _clean_string(payload.get("version"), "version", required=False, max_length=80) or "Main"
    return {
        "name": name,
        "label": label,
        "description": description,
        "version": version,
    }


def _clean_string(
    value: Any,
    field_name: str,
    *,
    required: bool,
    max_length: int,
) -> str:
    if value is None:
        if required:
            raise ApiError(422, "validation_error", f"Missing required field: {field_name}")
        return ""
    if not isinstance(value, str):
        raise ApiError(422, "validation_error", f"Field must be a string: {field_name}")
    cleaned = value.strip()
    if required and not cleaned:
        raise ApiError(422, "validation_error", f"Field cannot be empty: {field_name}")
    if len(cleaned) > max_length:
        raise ApiError(
            422,
            "validation_error",
            f"Field is too long: {field_name}",
            {"max_length": max_length},
        )
    return cleaned


def _validate_id(field_name: str, value: str) -> str:
    cleaned = str(value or "").strip()
    if not ID_PATTERN.match(cleaned):
        raise ApiError(422, "validation_error", f"Invalid {field_name}")
    return cleaned


def _bool_query(name: str, *, default: bool) -> bool:
    raw_value = request.args.get(name)
    if raw_value is None or raw_value == "":
        return default
    normalized = raw_value.strip().lower()
    if normalized in ("1", "true", "yes"):
        return True
    if normalized in ("0", "false", "no"):
        return False
    raise ApiError(400, "invalid_query_parameter", f"Invalid boolean query parameter: {name}")


def _load_pipeline_graph(neo4j_api_base_url: str) -> dict[str, Any]:
    try:
        graph = run_async(
            fetch_pipeline_graph(
                neo4j_api_base_url,
                authorization=_request_authorization_header(),
            )
        )
    except Exception as error:
        _log_event("pipeline_graph_load_failed", error_type=type(error).__name__)
        raise ApiError(500, "backend_unavailable", "Pipeline backend unavailable") from error
    return graph if isinstance(graph, dict) else {}


def _load_pipeline_versions(
    neo4j_api_base_url: str,
    *,
    include_graph: bool,
) -> list[dict[str, Any]]:
    try:
        versions = run_async(
            fetch_pipeline_versions(
                neo4j_api_base_url,
                include_graph=include_graph,
                authorization=_request_authorization_header(),
            )
        )
    except Exception as error:
        _log_event("pipeline_versions_load_failed", error_type=type(error).__name__)
        raise ApiError(500, "backend_unavailable", "Pipeline backend unavailable") from error
    return versions if isinstance(versions, list) else []


def _create_pipeline(
    neo4j_api_base_url: str,
    payload: dict[str, str],
) -> dict[str, Any]:
    api_url = f"{neo4j_api_base_url.rstrip('/')}/neo4j_update_pipeline_overview"
    try:
        headers = {}
        authorization = _request_authorization_header()
        if authorization:
            headers["Authorization"] = authorization
        response = requests.post(api_url, json=payload, headers=headers, timeout=30)
    except requests.RequestException as error:
        _log_event("pipeline_create_failed", error_type=type(error).__name__)
        raise ApiError(500, "backend_unavailable", "Pipeline backend unavailable") from error

    if response.status_code >= 500:
        raise ApiError(500, "backend_unavailable", "Pipeline backend unavailable")
    if response.status_code >= 400:
        raise ApiError(
            response.status_code if response.status_code in (400, 404, 422) else 400,
            "backend_rejected_request",
            "Pipeline backend rejected the request",
        )

    graph = _load_pipeline_graph(neo4j_api_base_url)
    pipeline = _pipeline_summary_from_graph(graph)
    if pipeline:
        return pipeline

    response_payload = _safe_response_json(response)
    return {
        "id": str(response_payload.get("pipeline_uid") or "design"),
        "name": payload["name"],
        "label": payload["label"],
        "description": payload["description"],
        "status": "design",
        "version": payload["version"],
        "active_version_id": str(response_payload.get("active_version_uid") or "main"),
        "active_version_name": payload["version"],
        "created_at": response_payload.get("created_at"),
        "updated_at": response_payload.get("updated_at"),
        "node_count": 0,
        "edge_count": 0,
        "step_count": 0,
    }


def _safe_response_json(response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _pipeline_summary_from_graph(graph: dict[str, Any]) -> dict[str, Any] | None:
    pipeline = graph.get("pipeline") if isinstance(graph.get("pipeline"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []

    pipeline_id = str(pipeline.get("uid") or "").strip()
    if not pipeline_id and not nodes:
        return None

    updated_at = graph.get("updated_at") or pipeline.get("updated_at")
    name = str(pipeline.get("name") or pipeline.get("label") or "Design pipeline").strip()
    label = str(pipeline.get("label") or name).strip()
    active_version_name = str(
        pipeline.get("active_version_name")
        or pipeline.get("version")
        or "Main"
    ).strip()

    return {
        "id": pipeline_id or "design",
        "name": name,
        "label": label,
        "description": str(pipeline.get("description") or ""),
        "status": str(pipeline.get("status") or "design"),
        "version": str(pipeline.get("version") or active_version_name or "Main"),
        "active_version_id": str(pipeline.get("active_version_uid") or "main"),
        "active_version_name": active_version_name,
        "created_at": pipeline.get("created_at"),
        "updated_at": updated_at,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "step_count": int(pipeline.get("step_count") or len(nodes)),
    }


def _pipeline_id_matches(pipeline: dict[str, Any], requested_id: str) -> bool:
    aliases = {
        str(pipeline.get("id") or ""),
        str(pipeline.get("active_version_id") or ""),
    }
    aliases.discard("")
    if pipeline.get("id") == "design":
        aliases.add("design")
    return requested_id in aliases


def _pipeline_graph_or_404(
    neo4j_api_base_url: str,
    pipeline_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    requested_id = _validate_id("pipeline_id", pipeline_id)
    graph = _load_pipeline_graph(neo4j_api_base_url)
    pipeline = _pipeline_summary_from_graph(graph)
    if not pipeline or not _pipeline_id_matches(pipeline, requested_id):
        raise ApiError(404, "pipeline_not_found", "Pipeline not found")
    return graph, pipeline


def _pipeline_version_graph_or_404(
    neo4j_api_base_url: str,
    pipeline_id: str,
    version_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    active_graph, pipeline = _pipeline_graph_or_404(neo4j_api_base_url, pipeline_id)
    requested_version_id = _validate_id("version_id", version_id)
    versions = _load_pipeline_versions(neo4j_api_base_url, include_graph=True)
    if not versions:
        active_version = _active_graph_version(active_graph, pipeline)
        if _version_id_matches(active_version, requested_version_id):
            return active_graph, pipeline, active_version
        raise ApiError(404, "pipeline_version_not_found", "Pipeline version not found")

    for version in versions:
        if not _version_id_matches(version, requested_version_id):
            continue
        version_graph = version.get("graph") if isinstance(version.get("graph"), dict) else active_graph
        return version_graph, pipeline, version

    raise ApiError(404, "pipeline_version_not_found", "Pipeline version not found")


def _version_id_matches(version: dict[str, Any], requested_version_id: str) -> bool:
    aliases = {
        str(version.get("uid") or ""),
        str(version.get("id") or ""),
        str(version.get("name") or ""),
        str(version.get("version") or ""),
    }
    if bool(version.get("is_main")) or str(version.get("uid") or "") == "main":
        aliases.add("main")
        aliases.add("Main")
    aliases.discard("")
    return requested_version_id in aliases


def _build_dockerfile_artifacts_or_error(graph: dict[str, Any]) -> dict[str, Any]:
    try:
        from deployment_agents import generate_dockerfiles_with_agent

        steps = extract_pipeline_steps(graph)
        file_refs = [
            file_ref
            for step in steps
            for file_ref in step.get("files", [])
        ]
        filenames = [file_ref["filename"] for file_ref in file_refs]
        ids = [file_ref["step_id"] for file_ref in file_refs]
        dockerfiles = run_async(
            generate_dockerfiles_with_agent(
                filenames,
                ids,
                None,
                pipeline_graph=graph,
                file_refs=file_refs,
            )
        )
        if hasattr(dockerfiles, "model_dump"):
            return dockerfiles.model_dump()
        return dockerfiles.dict()
    except (ValueError, DeploymentArtifactValidationError) as error:
        raise ApiError(
            422,
            "artifact_generation_failed",
            str(error),
        ) from error


def _build_argo_workflow_yaml_or_error(graph: dict[str, Any]) -> str:
    dockerfiles = _build_dockerfile_artifacts_or_error(graph)
    try:
        return build_argo_workflow_yaml(graph, dockerfiles)
    except (ValueError, DeploymentArtifactValidationError) as error:
        raise ApiError(
            422,
            "artifact_generation_failed",
            str(error),
        ) from error


def _public_version(version: dict[str, Any]) -> dict[str, Any]:
    modified_at = version.get("updated_at") or version.get("created_at")
    version_name = str(version.get("name") or version.get("version") or "Main")
    return {
        "id": str(version.get("uid") or "main"),
        "name": version_name,
        "version_name": version_name,
        "version": _version_from_modified_at(modified_at),
        "is_main": bool(version.get("is_main")),
        "created_at": version.get("created_at"),
        "modified_at": modified_at,
        "node_count": int(version.get("node_count") or 0),
        "edge_count": int(version.get("edge_count") or 0),
        "file_count": int(version.get("file_count") or 0),
    }


def _active_graph_version(
    graph: dict[str, Any],
    pipeline: dict[str, Any],
) -> dict[str, Any]:
    return {
        "uid": pipeline.get("active_version_id") or "main",
        "name": pipeline.get("active_version_name") or "Main",
        "version": pipeline.get("version") or "Main",
        "is_main": pipeline.get("active_version_id") in ("", None, "main"),
        "created_at": pipeline.get("created_at"),
        "updated_at": pipeline.get("updated_at") or graph.get("updated_at"),
        "node_count": pipeline.get("node_count") or 0,
        "edge_count": pipeline.get("edge_count") or 0,
        "file_count": len(_file_refs_from_graph(graph)),
        "graph": graph,
    }


def _list_workflows(
    neo4j_api_base_url: str,
    *,
    include_download_urls: bool,
) -> list[dict[str, Any]]:
    graph = _load_pipeline_graph(neo4j_api_base_url)
    pipeline = _pipeline_summary_from_graph(graph)
    if not pipeline:
        return []

    versions = _load_pipeline_versions(
        neo4j_api_base_url,
        include_graph=include_download_urls,
    )
    if not versions:
        versions = [_active_graph_version(graph, pipeline)]

    workflows = []
    for version in versions:
        version_graph = version.get("graph") if isinstance(version.get("graph"), dict) else graph
        workflows.append(
            _workflow_from_version(
                pipeline,
                version,
                version_graph,
                include_download_urls=include_download_urls,
            )
        )
    return workflows


def _workflow_from_version(
    pipeline: dict[str, Any],
    version: dict[str, Any],
    graph: dict[str, Any],
    *,
    include_download_urls: bool,
) -> dict[str, Any]:
    version_id = str(version.get("uid") or pipeline.get("active_version_id") or "main")
    modified_at = version.get("updated_at") or version.get("created_at") or pipeline.get("updated_at")
    version_name = str(version.get("name") or version.get("version") or pipeline.get("version") or "Main")
    derived_version = _version_from_modified_at(modified_at)
    workflow_id = _workflow_id(pipeline["id"], version_id, derived_version)
    access_urls = _access_urls_from_graph(graph) if include_download_urls else []

    return {
        "id": workflow_id,
        "workflow_id": workflow_id,
        "name": f"{pipeline['name']} - {version_name}",
        "pipeline_id": pipeline["id"],
        "pipeline_ids": [pipeline["id"]],
        "version_id": version_id,
        "version_name": version_name,
        "version": derived_version,
        "modified_at": modified_at,
        "node_count": int(version.get("node_count") or pipeline.get("node_count") or 0),
        "edge_count": int(version.get("edge_count") or pipeline.get("edge_count") or 0),
        "file_count": int(version.get("file_count") or len(_file_refs_from_graph(graph))),
        "download_url": access_urls[0]["url"] if access_urls else None,
        "access_urls": access_urls,
    }


def _workflow_id(pipeline_id: str, version_id: str, derived_version: str) -> str:
    raw_id = f"workflow-{pipeline_id}-{version_id}-{derived_version}"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw_id).strip("-")[:180]


def _version_from_modified_at(modified_at: Any) -> str:
    parsed = _parse_datetime(modified_at)
    if parsed is None:
        return "v-unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return f"v{parsed.strftime('%Y%m%dT%H%M%SZ')}"


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass

    match = re.match(
        r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
        r"(?P<fraction>\.\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?$",
        text,
    )
    if not match:
        return None

    fraction = match.group("fraction") or ""
    if fraction:
        fraction_digits = fraction[1:7].ljust(6, "0")
        fraction = f".{fraction_digits}"
    timezone_text = match.group("tz") or ""
    if timezone_text == "Z":
        timezone_text = "+00:00"
    elif timezone_text and ":" not in timezone_text:
        timezone_text = f"{timezone_text[:3]}:{timezone_text[3:]}"

    try:
        return datetime.fromisoformat(f"{match.group('base')}{fraction}{timezone_text}")
    except ValueError:
        return None


def _file_refs_from_graph(graph: dict[str, Any]) -> list[dict[str, str]]:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for node in nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else node
        step_id = str(data.get("flow_id") or node.get("id") or data.get("id") or "").strip()
        if not step_id:
            continue

        default_bucket = f"files-step-id-{step_id}".lower()
        raw_files = data.get("file_buckets") if isinstance(data.get("file_buckets"), list) else data.get("files")
        if not isinstance(raw_files, list):
            continue

        for item in raw_files:
            file_ref = _file_ref_from_item(item, default_bucket, step_id)
            if not file_ref:
                continue
            key = (file_ref["step_id"], file_ref["bucket"], file_ref["object_name"])
            if key in seen:
                continue
            seen.add(key)
            refs.append(file_ref)
    return refs


def _file_ref_from_item(
    item: Any,
    default_bucket: str,
    step_id: str,
) -> dict[str, str] | None:
    filename = ""
    bucket = default_bucket
    object_name = ""

    if isinstance(item, str):
        filename = item.strip()
        object_name = filename
    elif isinstance(item, dict):
        filename = str(item.get("filename") or item.get("name") or item.get("object_name") or "").strip()
        bucket = str(item.get("snapshot_bucket") or item.get("bucket") or default_bucket).strip().lower()
        object_name = str(item.get("snapshot_object") or item.get("object_name") or filename).strip()

    if not filename or not bucket or not object_name:
        return None
    return {
        "step_id": step_id,
        "filename": filename,
        "bucket": bucket,
        "object_name": object_name,
    }


def _access_urls_from_graph(graph: dict[str, Any]) -> list[dict[str, Any]]:
    access_urls: list[dict[str, Any]] = []
    for file_ref in _file_refs_from_graph(graph):
        signed_url = generate_signed_url(
            file_ref["bucket"],
            file_ref["object_name"],
            expires_seconds=SIGNED_URL_EXPIRES_SECONDS,
        )
        if not signed_url:
            continue
        access_urls.append({
            "step_id": file_ref["step_id"],
            "name": file_ref["filename"],
            "url": signed_url,
            "expires_in_seconds": SIGNED_URL_EXPIRES_SECONDS,
        })
    return access_urls


def generate_signed_url(
    bucket_name: str,
    object_name: str,
    *,
    expires_seconds: int = SIGNED_URL_EXPIRES_SECONDS,
) -> str | None:
    bucket = str(bucket_name or "").strip()
    object_key = str(object_name or "").strip()
    if not bucket or not object_key:
        return None
    try:
        client = get_minio_client()
        return client.presigned_get_object(
            bucket,
            object_key,
            expires=timedelta(seconds=expires_seconds),
        )
    except Exception as error:
        _log_event("signed_url_generation_failed", error_type=type(error).__name__)
        return None


def build_openapi_schema() -> dict[str, Any]:
    protected_responses = {
        "400": {"$ref": "#/components/responses/BadRequest"},
        "401": {"$ref": "#/components/responses/Unauthorized"},
        "403": {"$ref": "#/components/responses/Forbidden"},
        "422": {"$ref": "#/components/responses/ValidationError"},
        "500": {"$ref": "#/components/responses/InternalError"},
    }
    not_found_responses = {
        **protected_responses,
        "404": {"$ref": "#/components/responses/NotFound"},
    }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "inLUMEN Public API",
            "version": API_VERSION,
            "description": "Public API for pipeline, workflow, and deployment artifact access.",
        },
        "servers": [{"url": "/"}],
        "tags": [
            {"name": "Pipelines", "description": "Pipeline creation, lookup, listing, and versions."},
            {"name": "Artifacts", "description": "Generated Dockerfiles and Argo Workflow YAML for pipelines."},
            {"name": "Workflows", "description": "Argo Workflow metadata and version discovery."},
            {"name": "Health", "description": "Public service health and readiness checks."},
        ],
        "security": [{"bearerAuth": []}],
        "paths": {
            "/health": {
                "get": {
                    "tags": ["Health"],
                    "summary": "Health check",
                    "operationId": "health",
                    "security": [],
                    "responses": {
                        "200": {
                            "description": "The service is alive.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/HealthResponse"}
                                }
                            },
                        }
                    },
                }
            },
            "/ready": {
                "get": {
                    "tags": ["Health"],
                    "summary": "Readiness check",
                    "operationId": "readiness",
                    "security": [],
                    "responses": {
                        "200": {
                            "description": "The service is ready.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ReadinessResponse"}
                                }
                            },
                        },
                        "503": {"description": "The service is not ready."},
                    },
                }
            },
            "/openapi.json": {
                "get": {
                    "tags": ["Health"],
                    "summary": "OpenAPI JSON schema",
                    "operationId": "openapiJson",
                    "responses": {
                        "200": {
                            "description": "OpenAPI 3 schema.",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            },
                        },
                        "401": {"$ref": "#/components/responses/Unauthorized"},
                        "403": {"$ref": "#/components/responses/Forbidden"},
                        "500": {"$ref": "#/components/responses/InternalError"},
                    },
                }
            },
            "/api/v1/pipelines": {
                "get": {
                    "tags": ["Pipelines"],
                    "summary": "List pipelines",
                    "operationId": "listPipelines",
                    "responses": {
                        "200": {
                            "description": "Pipeline list.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PipelineListResponse"}
                                }
                            },
                        },
                        **protected_responses,
                    },
                },
                "post": {
                    "tags": ["Pipelines"],
                    "summary": "Create the design pipeline",
                    "operationId": "createPipeline",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/PipelineCreateRequest"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Pipeline created.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PipelineResponse"}
                                }
                            },
                        },
                        **protected_responses,
                    },
                },
            },
            "/api/v1/pipelines/{pipeline_id}": {
                "get": {
                    "tags": ["Pipelines"],
                    "summary": "Fetch a pipeline",
                    "operationId": "getPipeline",
                    "parameters": [{"$ref": "#/components/parameters/PipelineId"}],
                    "responses": {
                        "200": {
                            "description": "Pipeline and graph.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PipelineGraphResponse"}
                                }
                            },
                        },
                        **not_found_responses,
                    },
                }
            },
            "/api/v1/pipelines/{pipeline_id}/versions": {
                "get": {
                    "tags": ["Pipelines"],
                    "summary": "List pipeline versions",
                    "operationId": "listPipelineVersions",
                    "parameters": [{"$ref": "#/components/parameters/PipelineId"}],
                    "responses": {
                        "200": {
                            "description": "Pipeline versions.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PipelineVersionListResponse"}
                                }
                            },
                        },
                        **not_found_responses,
                    },
                }
            },
            "/api/v1/pipelines/{pipeline_id}/artifacts/dockerfiles": {
                "get": {
                    "tags": ["Artifacts"],
                    "summary": "Generate Dockerfiles for a pipeline",
                    "operationId": "getPipelineDockerfiles",
                    "parameters": [{"$ref": "#/components/parameters/PipelineId"}],
                    "responses": {
                        "200": {
                            "description": "Generated Dockerfiles for the active pipeline graph.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/DockerfileArtifactsResponse"}
                                }
                            },
                        },
                        **not_found_responses,
                    },
                }
            },
            "/api/v1/pipelines/{pipeline_id}/artifacts/argo-workflow.yaml": {
                "get": {
                    "tags": ["Artifacts"],
                    "summary": "Generate Argo Workflow YAML for a pipeline",
                    "operationId": "getPipelineArgoWorkflowYaml",
                    "parameters": [{"$ref": "#/components/parameters/PipelineId"}],
                    "responses": {
                        "200": {
                            "description": "Generated Argo Workflow YAML for the active pipeline graph.",
                            "content": {
                                "application/x-yaml": {
                                    "schema": {"type": "string"}
                                }
                            },
                        },
                        **not_found_responses,
                    },
                }
            },
            "/api/v1/pipelines/{pipeline_id}/versions/{version_id}/artifacts/dockerfiles": {
                "get": {
                    "tags": ["Artifacts"],
                    "summary": "Generate Dockerfiles for a pipeline version",
                    "operationId": "getPipelineVersionDockerfiles",
                    "parameters": [
                        {"$ref": "#/components/parameters/PipelineId"},
                        {"$ref": "#/components/parameters/VersionId"},
                    ],
                    "responses": {
                        "200": {
                            "description": "Generated Dockerfiles for the selected pipeline version.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/DockerfileArtifactsResponse"}
                                }
                            },
                        },
                        **not_found_responses,
                    },
                }
            },
            "/api/v1/pipelines/{pipeline_id}/versions/{version_id}/artifacts/argo-workflow.yaml": {
                "get": {
                    "tags": ["Artifacts"],
                    "summary": "Generate Argo Workflow YAML for a pipeline version",
                    "operationId": "getPipelineVersionArgoWorkflowYaml",
                    "parameters": [
                        {"$ref": "#/components/parameters/PipelineId"},
                        {"$ref": "#/components/parameters/VersionId"},
                    ],
                    "responses": {
                        "200": {
                            "description": "Generated Argo Workflow YAML for the selected pipeline version.",
                            "content": {
                                "application/x-yaml": {
                                    "schema": {"type": "string"}
                                }
                            },
                        },
                        **not_found_responses,
                    },
                }
            },
            "/api/v1/workflows": {
                "get": {
                    "tags": ["Workflows"],
                    "summary": "List available Argo Workflows",
                    "operationId": "listWorkflows",
                    "parameters": [{"$ref": "#/components/parameters/IncludeDownloadUrls"}],
                    "responses": {
                        "200": {
                            "description": "Available workflow metadata.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/WorkflowListResponse"}
                                }
                            },
                        },
                        **protected_responses,
                    },
                }
            },
            "/api/v1/workflows/versions": {
                "get": {
                    "tags": ["Workflows"],
                    "summary": "List workflow versions derived from modification dates",
                    "operationId": "listWorkflowVersions",
                    "responses": {
                        "200": {
                            "description": "Workflow versions.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/WorkflowVersionListResponse"}
                                }
                            },
                        },
                        **protected_responses,
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT or opaque API token",
                    "description": "Use `Authorization: Bearer <token>`. With `AUTH_ENABLED=true`, this must be a Keycloak JWT. Otherwise, use `API_AUTH_TOKEN`.",
                }
            },
            "parameters": {
                "PipelineId": {
                    "name": "pipeline_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 160},
                },
                "VersionId": {
                    "name": "version_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 160},
                },
                "IncludeDownloadUrls": {
                    "name": "include_download_urls",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "boolean", "default": False},
                },
            },
            "responses": {
                "BadRequest": {"description": "Bad request.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                "Unauthorized": {"description": "Missing bearer token.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                "Forbidden": {"description": "Invalid bearer token.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                "NotFound": {"description": "Resource not found.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                "ValidationError": {"description": "Validation failed.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                "InternalError": {"description": "Internal server error.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
            },
            "schemas": {
                "ErrorResponse": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {
                        "error": {
                            "type": "object",
                            "required": ["code", "message"],
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                                "details": {"type": "object"},
                            },
                        }
                    },
                },
                "HealthResponse": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string", "example": "ok"}},
                },
                "ReadinessResponse": {
                    "type": "object",
                    "required": ["status", "checks"],
                    "properties": {
                        "status": {"type": "string"},
                        "checks": {"type": "object", "additionalProperties": {"type": "string"}},
                    },
                },
                "PipelineCreateRequest": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "maxLength": 120},
                        "label": {"type": "string", "maxLength": 120},
                        "description": {"type": "string", "maxLength": 1000},
                        "version": {"type": "string", "maxLength": 80, "default": "Main"},
                    },
                    "additionalProperties": False,
                },
                "Pipeline": {
                    "type": "object",
                    "required": ["id", "name", "status", "node_count", "edge_count"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                        "status": {"type": "string"},
                        "version": {"type": "string"},
                        "active_version_id": {"type": "string"},
                        "active_version_name": {"type": "string"},
                        "created_at": {"type": "string", "nullable": True},
                        "updated_at": {"type": "string", "nullable": True},
                        "node_count": {"type": "integer"},
                        "edge_count": {"type": "integer"},
                        "step_count": {"type": "integer"},
                    },
                },
                "PipelineResponse": {
                    "type": "object",
                    "required": ["pipeline"],
                    "properties": {"pipeline": {"$ref": "#/components/schemas/Pipeline"}},
                },
                "PipelineListResponse": {
                    "type": "object",
                    "required": ["pipelines"],
                    "properties": {
                        "pipelines": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Pipeline"},
                        }
                    },
                },
                "PipelineGraphResponse": {
                    "type": "object",
                    "required": ["pipeline", "graph"],
                    "properties": {
                        "pipeline": {"$ref": "#/components/schemas/Pipeline"},
                        "graph": {"type": "object"},
                    },
                },
                "PipelineVersion": {
                    "type": "object",
                    "required": ["id", "version", "modified_at"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "version_name": {"type": "string"},
                        "version": {"type": "string", "description": "Version derived from modification date."},
                        "is_main": {"type": "boolean"},
                        "created_at": {"type": "string", "nullable": True},
                        "modified_at": {"type": "string", "nullable": True},
                        "node_count": {"type": "integer"},
                        "edge_count": {"type": "integer"},
                        "file_count": {"type": "integer"},
                    },
                },
                "PipelineVersionListResponse": {
                    "type": "object",
                    "required": ["pipeline_id", "versions"],
                    "properties": {
                        "pipeline_id": {"type": "string"},
                        "versions": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/PipelineVersion"},
                        },
                    },
                },
                "DockerfileArtifact": {
                    "type": "object",
                    "required": ["dockerfile_filename", "content", "flow_id", "image", "command", "files"],
                    "properties": {
                        "dockerfile_filename": {"type": "string"},
                        "content": {"type": "string"},
                        "flow_id": {"type": "string"},
                        "image": {"type": "string"},
                        "command": {"type": "array", "items": {"type": "string"}},
                        "files": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "DockerfileArtifactsResponse": {
                    "type": "object",
                    "required": ["pipeline_id", "version_id", "version_name", "dockerfiles", "guardrails"],
                    "properties": {
                        "pipeline_id": {"type": "string"},
                        "version_id": {"type": "string"},
                        "version_name": {"type": "string"},
                        "dockerfiles": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/DockerfileArtifact"},
                        },
                        "guardrails": {
                            "type": "object",
                            "properties": {
                                "valid": {"type": "boolean"},
                                "checks": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
                "AccessUrl": {
                    "type": "object",
                    "required": ["name", "url", "expires_in_seconds"],
                    "properties": {
                        "step_id": {"type": "string"},
                        "name": {"type": "string"},
                        "url": {"type": "string", "format": "uri"},
                        "expires_in_seconds": {"type": "integer"},
                    },
                },
                "Workflow": {
                    "type": "object",
                    "required": ["id", "pipeline_id", "pipeline_ids", "version", "modified_at"],
                    "properties": {
                        "id": {"type": "string"},
                        "workflow_id": {"type": "string"},
                        "name": {"type": "string"},
                        "pipeline_id": {"type": "string"},
                        "pipeline_ids": {"type": "array", "items": {"type": "string"}},
                        "version_id": {"type": "string"},
                        "version_name": {"type": "string"},
                        "version": {"type": "string"},
                        "modified_at": {"type": "string", "nullable": True},
                        "node_count": {"type": "integer"},
                        "edge_count": {"type": "integer"},
                        "file_count": {"type": "integer"},
                        "download_url": {"type": "string", "format": "uri", "nullable": True},
                        "access_urls": {"type": "array", "items": {"$ref": "#/components/schemas/AccessUrl"}},
                    },
                },
                "WorkflowListResponse": {
                    "type": "object",
                    "required": ["workflows"],
                    "properties": {
                        "workflows": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Workflow"},
                        }
                    },
                },
                "WorkflowVersionListResponse": {
                    "type": "object",
                    "required": ["versions"],
                    "properties": {
                        "versions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "workflow_id": {"type": "string"},
                                    "pipeline_id": {"type": "string"},
                                    "version": {"type": "string"},
                                    "version_id": {"type": "string"},
                                    "version_name": {"type": "string"},
                                    "modified_at": {"type": "string", "nullable": True},
                                },
                            },
                        }
                    },
                },
            },
        },
    }


SWAGGER_UI_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>inLUMEN Public API Docs</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body { margin: 0; background: #f7f7f7; color: #1f2933; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .auth-panel { max-width: 520px; margin: 64px auto 24px; padding: 24px; background: #fff; border: 1px solid #d8dee4; border-radius: 8px; box-shadow: 0 8px 24px rgba(31, 41, 51, 0.08); }
    .auth-panel h1 { margin: 0 0 8px; font-size: 22px; line-height: 1.25; }
    .auth-panel p { margin: 0 0 16px; color: #52616f; line-height: 1.5; }
    .auth-row { display: flex; gap: 8px; }
    .auth-row input { flex: 1; min-width: 0; padding: 10px 12px; border: 1px solid #b7c1cc; border-radius: 6px; font: inherit; }
    .auth-row button { padding: 10px 14px; border: 0; border-radius: 6px; background: #0b5cad; color: #fff; font: inherit; cursor: pointer; }
    .auth-error { min-height: 20px; margin-top: 12px; color: #b42318; }
    #swagger-ui { background: #fff; min-height: 100vh; }
  </style>
</head>
<body>
  <section id="auth-panel" class="auth-panel">
    <h1>inLUMEN Public API</h1>
    <p>Enter a bearer token to load the live Swagger documentation. With Keycloak enabled, use a Keycloak access token; otherwise use API_AUTH_TOKEN.</p>
    <form id="auth-form" class="auth-row">
      <input id="auth-token" type="password" autocomplete="current-password" placeholder="Bearer token" aria-label="Bearer token">
      <button type="submit">Open docs</button>
    </form>
    <div id="auth-error" class="auth-error" role="alert"></div>
  </section>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    (function () {
      var storageKey = "inlumen_api_auth_token";
      var authPanel = document.getElementById("auth-panel");
      var authForm = document.getElementById("auth-form");
      var tokenInput = document.getElementById("auth-token");
      var errorBox = document.getElementById("auth-error");

      function setError(message) {
        errorBox.textContent = message || "";
      }

      async function loadSwagger(token) {
        setError("");
        var response = await fetch("/openapi.json", {
          headers: { "Authorization": "Bearer " + token }
        });
        if (!response.ok) {
          sessionStorage.removeItem(storageKey);
          setError(response.status === 401 || response.status === 403 ? "Invalid API token." : "Documentation is unavailable.");
          return;
        }
        var spec = await response.json();
        sessionStorage.setItem(storageKey, token);
        authPanel.hidden = true;
        var ui = SwaggerUIBundle({
          spec: spec,
          dom_id: "#swagger-ui",
          deepLinking: true,
          persistAuthorization: true,
          tryItOutEnabled: true,
          requestInterceptor: function (request) {
            var currentToken = sessionStorage.getItem(storageKey);
            if (currentToken && !request.headers.Authorization) {
              request.headers.Authorization = "Bearer " + currentToken;
            }
            return request;
          }
        });
        window.ui = ui;
        try {
          ui.preauthorizeApiKey("bearerAuth", token);
        } catch (error) {}
      }

      authForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var token = tokenInput.value.trim();
        if (!token) {
          setError("Token is required.");
          return;
        }
        loadSwagger(token);
      });

      var savedToken = sessionStorage.getItem(storageKey);
      if (savedToken) {
        tokenInput.value = savedToken;
        loadSwagger(savedToken);
      }
    })();
  </script>
</body>
</html>
"""
