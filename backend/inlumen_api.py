import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, make_response, request

from analytics_api import (
    agentic_generate_dagster_bundle,
    agentic_generate_dagster_definitions,
    agentic_generate_dockerfiles,
    agentic_generate_version_yamls,
    agentic_generate_yaml,
    agentic_pipeline_editor,
    agentic_pipeline_editor_reset,
)
from auth_middleware import require_auth
from graph_client import dispatch_graph_request
from local_api_client import LocalApiResponse
from object_client import dispatch_object_request
from public_api import create_public_api_blueprint
from runtime_config import add_cors_headers, get_service_port


INLUMEN_API_PORT = get_service_port("INLUMEN_API_PORT", 5000)
CHATBOT_CONFIGS_PATH = Path(
    os.getenv("CHATBOT_CONFIGS_PATH", "state/chatbot_configurations.json")
)

app = Flask(__name__)
app.register_blueprint(create_public_api_blueprint())
app.add_url_rule(
    "/agentic_generate_dockerfiles",
    endpoint="agentic_generate_dockerfiles",
    view_func=agentic_generate_dockerfiles,
    methods=["POST", "OPTIONS"],
)
app.add_url_rule(
    "/agentic_generate_yaml",
    endpoint="agentic_generate_yaml",
    view_func=agentic_generate_yaml,
    methods=["POST", "OPTIONS"],
)
app.add_url_rule(
    "/agentic_generate_dagster_bundle",
    endpoint="agentic_generate_dagster_bundle",
    view_func=agentic_generate_dagster_bundle,
    methods=["POST", "OPTIONS"],
)
app.add_url_rule(
    "/agentic_generate_dagster_definitions",
    endpoint="agentic_generate_dagster_definitions",
    view_func=agentic_generate_dagster_definitions,
    methods=["POST", "OPTIONS"],
)
app.add_url_rule(
    "/agentic_generate_version_yamls",
    endpoint="agentic_generate_version_yamls",
    view_func=agentic_generate_version_yamls,
    methods=["GET", "POST", "OPTIONS"],
)
app.add_url_rule(
    "/simple_chat",
    endpoint="simple_chat",
    view_func=agentic_pipeline_editor,
    methods=["POST", "OPTIONS"],
)
app.add_url_rule(
    "/agentic_pipeline_editor",
    endpoint="agentic_pipeline_editor",
    view_func=agentic_pipeline_editor,
    methods=["POST", "OPTIONS"],
)
app.add_url_rule(
    "/simple_chat/reset",
    endpoint="simple_chat_reset",
    view_func=agentic_pipeline_editor_reset,
    methods=["POST", "OPTIONS"],
)
app.add_url_rule(
    "/agentic_pipeline_editor/reset",
    endpoint="agentic_pipeline_editor_reset",
    view_func=agentic_pipeline_editor_reset,
    methods=["POST", "OPTIONS"],
)


@app.after_request
def apply_cors(response):
    return add_cors_headers(response, request.headers.get("Origin"))


def _preflight_response():
    return make_response("", 200)


def _forward_headers(include_content_type: bool = True) -> dict[str, str]:
    headers: dict[str, str] = {}
    authorization = request.headers.get("Authorization")
    if authorization:
        headers["Authorization"] = authorization
    accept = request.headers.get("Accept")
    if accept:
        headers["Accept"] = accept
    if include_content_type and request.content_type:
        headers["Content-Type"] = request.content_type
    return headers


def _response_from_upstream(upstream: LocalApiResponse) -> Response:
    excluded_headers = {
        "connection",
        "content-encoding",
        "content-length",
        "transfer-encoding",
    }
    headers = [
        (name, value)
        for name, value in upstream.headers.items()
        if name.lower() not in excluded_headers
    ]
    return Response(upstream.content, status=upstream.status_code, headers=headers)


def _proxy(
    adapter_request,
    backend_path: str,
    *,
    method: str | None = None,
    params: dict[str, Any] | None = None,
    data: Any = None,
    json_payload: Any = None,
    files: Any = None,
    form: dict[str, Any] | None = None,
) -> LocalApiResponse:
    include_content_type = files is None and form is None and json_payload is None
    body = None if json_payload is not None else data if data is not None else request.get_data()
    return adapter_request(
        backend_path,
        method=method or request.method,
        params=params if params is not None else request.args,
        data=body,
        json_payload=json_payload,
        files=files,
        form=form,
        headers=_forward_headers(include_content_type=include_content_type),
    )


def _proxy_response(adapter_request, backend_path: str) -> Response:
    if request.method == "OPTIONS":
        return _preflight_response()
    return _response_from_upstream(_proxy(adapter_request, backend_path))


def _json_error(status_code: int, message: str, details: Any = None):
    payload = {"error": message}
    if details is not None:
        payload["details"] = details
    return jsonify(payload), status_code


def _upstream_json(upstream: LocalApiResponse) -> Any:
    try:
        return upstream.json()
    except ValueError:
        return {"status": upstream.status_code, "text": upstream.text}


def _request_json() -> dict[str, Any]:
    value = request.get_json(silent=True) or {}
    return value if isinstance(value, dict) else {}


def _filename_from_request() -> str:
    data = _request_json()
    return str(
        data.get("filename")
        or request.form.get("filename")
        or request.args.get("filename")
        or ""
    ).strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_chatbot_configs() -> list[dict[str, Any]]:
    try:
        payload = json.loads(CHATBOT_CONFIGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []
    configs = payload.get("configs") if isinstance(payload, dict) else payload
    return configs if isinstance(configs, list) else []


def _save_chatbot_configs(configs: list[dict[str, Any]]) -> None:
    CHATBOT_CONFIGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHATBOT_CONFIGS_PATH.write_text(
        json.dumps({"configs": configs}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _chatbot_config_response(config: dict[str, Any]) -> dict[str, Any]:
    response = {
        "id": str(config.get("id") or ""),
        "name": str(config.get("name") or ""),
        "provider": str(config.get("provider") or "custom"),
        "model": str(config.get("model") or ""),
        "baseUrl": str(config.get("baseUrl") or config.get("base_url") or ""),
        "base_url": str(config.get("baseUrl") or config.get("base_url") or ""),
        "system_prompt": str(config.get("system_prompt") or ""),
        "temperature": config.get("temperature", 0.7),
        "created_at": config.get("created_at"),
        "updated_at": config.get("updated_at"),
    }
    if config.get("readOnly") or config.get("read_only"):
        response["readOnly"] = True
        response["read_only"] = True
    return response


def _validate_chatbot_config_payload(
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | tuple[Response, int]:
    existing = existing or {}
    name = str(payload.get("name") or existing.get("name") or "").strip()
    provider = str(payload.get("provider") or existing.get("provider") or "custom").strip()
    model = str(payload.get("model") or existing.get("model") or "").strip()
    base_url = str(
        payload.get("baseUrl")
        or payload.get("base_url")
        or existing.get("baseUrl")
        or existing.get("base_url")
        or ""
    ).strip()
    if not name:
        return _json_error(400, "name is required")
    if not model:
        return _json_error(400, "model is required")
    if not base_url:
        return _json_error(400, "baseUrl is required")
    now = _utc_now_iso()
    return {
        "id": str(existing.get("id") or payload.get("id") or uuid.uuid4()),
        "name": name,
        "provider": provider or "custom",
        "model": model,
        "baseUrl": base_url,
        "base_url": base_url,
        "system_prompt": str(payload.get("system_prompt") or existing.get("system_prompt") or ""),
        "temperature": payload.get("temperature", existing.get("temperature", 0.7)),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }


@app.route("/api/graph/nodes", methods=["POST", "DELETE", "OPTIONS"])
@require_auth
def graph_nodes():
    if request.method == "OPTIONS":
        return _preflight_response()
    if request.method == "POST":
        return _response_from_upstream(_proxy(dispatch_graph_request, "neo4j_add_node"))

    graph_response = _proxy(dispatch_graph_request, "neo4j_clear_nodes")
    if not graph_response.ok:
        return _response_from_upstream(graph_response)

    graph_payload = _upstream_json(graph_response)
    deleted_ids = graph_payload.get("deleted_step_flow_ids") if isinstance(graph_payload, dict) else []
    storage_cleanup = []
    for flow_id in deleted_ids or []:
        storage_response = _proxy(
            dispatch_object_request,
            "minio_clear_bucket",
            method="DELETE",
            params={"bucket_id": flow_id},
            data=b"",
        )
        storage_cleanup.append({
            "flow_id": flow_id,
            "status": storage_response.status_code,
            "ok": storage_response.ok,
        })

    if isinstance(graph_payload, dict):
        graph_payload["storage_cleanup"] = storage_cleanup
        return jsonify(graph_payload), graph_response.status_code
    return jsonify({"graph": graph_payload, "storage_cleanup": storage_cleanup}), graph_response.status_code


@app.route("/api/chatbot-configs", methods=["GET", "POST", "OPTIONS"])
@require_auth
def chatbot_configs():
    if request.method == "OPTIONS":
        return _preflight_response()

    configs = _load_chatbot_configs()
    if request.method == "GET":
        return jsonify({
            "configs": [
                _chatbot_config_response(config)
                for config in configs
            ]
        }), 200

    payload = _request_json()
    config = _validate_chatbot_config_payload(payload)
    if isinstance(config, tuple):
        return config
    configs.insert(0, config)
    _save_chatbot_configs(configs)
    return jsonify({"config": _chatbot_config_response(config)}), 201


@app.route("/api/chatbot-configs/<config_id>", methods=["GET", "PUT", "DELETE", "OPTIONS"])
@require_auth
def chatbot_config(config_id: str):
    if request.method == "OPTIONS":
        return _preflight_response()

    configs = _load_chatbot_configs()
    index = next(
        (idx for idx, config in enumerate(configs) if str(config.get("id")) == config_id),
        None,
    )
    if index is None:
        return _json_error(404, "chatbot config not found")

    if request.method == "GET":
        return jsonify({"config": _chatbot_config_response(configs[index])}), 200

    if request.method == "DELETE":
        deleted = configs.pop(index)
        _save_chatbot_configs(configs)
        return jsonify({"deleted_id": str(deleted.get("id") or config_id)}), 200

    config = _validate_chatbot_config_payload(_request_json(), existing=configs[index])
    if isinstance(config, tuple):
        return config
    configs[index] = config
    _save_chatbot_configs(configs)
    return jsonify({"config": _chatbot_config_response(config)}), 200


@app.route("/api/graph/nodes/<node_id>", methods=["DELETE", "OPTIONS"])
@require_auth
def graph_node(node_id: str):
    if request.method == "OPTIONS":
        return _preflight_response()

    graph_response = _proxy(dispatch_graph_request, f"neo4j_delete_node/{node_id}", data=b"")
    if not graph_response.ok:
        return _response_from_upstream(graph_response)

    storage_response = _proxy(
        dispatch_object_request,
        "minio_clear_bucket",
        method="DELETE",
        params={"bucket_id": node_id},
        data=b"",
    )
    status_code = graph_response.status_code
    return jsonify({
        "graph": _upstream_json(graph_response),
        "storage_cleanup": {
            "status": storage_response.status_code,
            "ok": storage_response.ok,
            "response": _upstream_json(storage_response),
        },
    }), status_code


@app.route("/api/graph/nodes/properties", methods=["POST", "OPTIONS"])
@require_auth
def graph_node_properties():
    return _proxy_response(dispatch_graph_request, "neo4j_update_node")


@app.route("/api/graph/nodes/position", methods=["POST", "OPTIONS"])
@require_auth
def graph_node_position():
    return _proxy_response(dispatch_graph_request, "neo4j_update_node_position")


@app.route("/api/graph/edges", methods=["POST", "DELETE", "OPTIONS"])
@require_auth
def graph_edges():
    if request.method == "POST":
        return _proxy_response(dispatch_graph_request, "neo4j_add_edge")
    return _proxy_response(dispatch_graph_request, "neo4j_delete_edge")


@app.route("/api/pipeline/graph", methods=["GET", "OPTIONS"])
@require_auth
def pipeline_graph():
    return _proxy_response(dispatch_graph_request, "neo4j_get_graph")


@app.route("/api/pipeline/updated-at", methods=["GET", "OPTIONS"])
@require_auth
def pipeline_updated_at():
    return _proxy_response(dispatch_graph_request, "neo4j_get_pipeline_updated_at")


@app.route("/api/pipeline/overview", methods=["GET", "POST", "OPTIONS"])
@require_auth
def pipeline_overview():
    if request.method == "GET":
        return _proxy_response(dispatch_graph_request, "neo4j_get_overview_properties")
    return _proxy_response(dispatch_graph_request, "neo4j_update_pipeline_overview")


@app.route("/api/pipeline/versions", methods=["GET", "POST", "DELETE", "OPTIONS"])
@require_auth
def pipeline_versions():
    if request.method == "GET":
        return _proxy_response(dispatch_graph_request, "neo4j_list_pipeline_versions")
    if request.method == "POST":
        return _proxy_response(dispatch_graph_request, "neo4j_save_pipeline_version")
    return _proxy_response(dispatch_graph_request, "neo4j_delete_pipeline_version")


@app.route("/api/pipeline/versions/main", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_version_main():
    return _proxy_response(dispatch_graph_request, "neo4j_save_pipeline_main")


@app.route("/api/pipeline/versions/active", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_version_active():
    return _proxy_response(dispatch_graph_request, "neo4j_save_pipeline_active_version")


@app.route("/api/pipeline/versions/restore", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_version_restore():
    return _proxy_response(dispatch_graph_request, "neo4j_restore_pipeline_version")


@app.route("/api/pipeline/versions/set-main", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_version_set_main():
    return _proxy_response(dispatch_graph_request, "neo4j_set_pipeline_version_as_main")


@app.route("/api/files", methods=["GET", "OPTIONS"])
@require_auth
def files_metadata():
    return _proxy_response(dispatch_graph_request, "neo4j_get_all_files")


@app.route("/api/files/content", methods=["GET", "OPTIONS"])
@require_auth
def file_content():
    if request.method == "OPTIONS":
        return _preflight_response()
    container_id = str(request.args.get("container_id") or "").strip()
    filename = str(request.args.get("filename") or "").strip()
    if not container_id or not filename:
        return _json_error(400, "container_id and filename are required")
    storage_response = _proxy(
        dispatch_object_request,
        "minio_read_file",
        method="GET",
        params={"bucket_id": container_id, "filename": filename},
        data=b"",
    )
    return _response_from_upstream(storage_response)


@app.route("/api/nodes/<node_id>/files", methods=["POST", "DELETE", "OPTIONS"])
@require_auth
def node_files(node_id: str):
    if request.method == "OPTIONS":
        return _preflight_response()

    if request.method == "POST":
        uploaded = request.files.get("file")
        if uploaded is None:
            return _json_error(400, "file is required")
        storage_response = _proxy(
            dispatch_object_request,
            "minio_upload_file",
            method="POST",
            params={},
            data=b"",
            form={"bucket_id": node_id},
            files={
                "file": (
                    uploaded.filename,
                    uploaded.stream,
                    uploaded.mimetype or "application/octet-stream",
                )
            },
        )
        if not storage_response.ok:
            return _response_from_upstream(storage_response)

        graph_response = _proxy(
            dispatch_graph_request,
            "neo4j_add_file",
            method="POST",
            params={},
            data=b"",
            json_payload={
                "properties": {
                    "flow_id": node_id,
                    "filename": uploaded.filename,
                }
            },
        )
        if not graph_response.ok:
            return _response_from_upstream(graph_response)

        return jsonify({
            "file": _upstream_json(storage_response),
            "graph": _upstream_json(graph_response),
        }), 200

    filename = _filename_from_request()
    if not filename:
        return _json_error(400, "filename is required")
    storage_response = _proxy(
        dispatch_object_request,
        "minio_remove_file",
        method="DELETE",
        params={},
        data=b"",
        form={"bucket_id": node_id, "filename": filename},
    )
    if not storage_response.ok:
        return _response_from_upstream(storage_response)

    graph_response = _proxy(
        dispatch_graph_request,
        "neo4j_delete_file",
        method="DELETE",
        params={},
        data=b"",
        json_payload={
            "properties": {
                "flow_id": node_id,
                "filename": filename,
            }
        },
    )
    if not graph_response.ok:
        return _response_from_upstream(graph_response)

    return jsonify({
        "file": _upstream_json(storage_response),
        "graph": _upstream_json(graph_response),
    }), 200


@app.route("/api/nodes/<node_id>/files/text", methods=["PUT", "OPTIONS"])
@require_auth
def node_text_file(node_id: str):
    if request.method == "OPTIONS":
        return _preflight_response()
    data = _request_json()
    filename = str(data.get("filename") or "").strip()
    content = data.get("content")
    container_id = str(data.get("container_id") or node_id).strip()
    if not filename:
        return _json_error(400, "filename is required")
    if not isinstance(content, str):
        return _json_error(400, "content must be a string")
    storage_response = _proxy(
        dispatch_object_request,
        "minio_update_text_file",
        method="PUT",
        params={},
        data=b"",
        json_payload={
            "bucket_id": container_id,
            "filename": filename,
            "content": content,
        },
    )
    return _response_from_upstream(storage_response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=INLUMEN_API_PORT)
