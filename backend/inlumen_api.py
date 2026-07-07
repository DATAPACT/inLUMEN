import csv
import json
import os
import uuid
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, make_response, request

from analytics_api import (
    agentic_generate_deployment_bundle,
    agentic_generate_dagster,
    agentic_generate_dockerfiles,
    agentic_generate_version_yamls,
    agentic_generate_yaml,
    agentic_pipeline_editor,
    agentic_pipeline_editor_reset,
)
from auth_middleware import require_auth
from chat_state import clear_state_from_disk
from generators.routes import create_generator_blueprint
from graph_client import dispatch_graph_request
from local_api_client import LocalApiResponse
from node_definitions import create_node_definitions_blueprint
from node_definitions.artifacts import configuration_hash
from object_client import dispatch_object_request
from provenance_provo import build_prov_o_jsonld, provenance_prov_o_filename
from provenance_report import build_provenance_pdf, provenance_report_filename
from public_api import create_public_api_blueprint
from runtime_config import add_cors_headers, get_service_port


INLUMEN_API_PORT = get_service_port("INLUMEN_API_PORT", 5000)
CODEGEN_SERVICE_URL = os.getenv(
    "INLUMEN_CODEGEN_SERVICE_URL",
    "http://127.0.0.1:8010",
).rstrip("/")
CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv("INLUMEN_CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS", "300")
)
CODEGEN_PIPELINE_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv("INLUMEN_CODEGEN_PIPELINE_REQUEST_TIMEOUT_SECONDS", "1200")
)
CODEGEN_RUNTIME_FILENAMES = {
    "main.py",
    "requirements.txt",
    "node-manifest.json",
    "validation-report.json",
}
PIPELINE_GENERATION_RUNS: dict[str, dict[str, Any]] = {}
CHATBOT_CONFIGS_PATH = Path(
    os.getenv("CHATBOT_CONFIGS_PATH", "state/chatbot_configurations.json")
)

app = Flask(__name__)
app.register_blueprint(create_public_api_blueprint())
app.register_blueprint(create_node_definitions_blueprint())
app.register_blueprint(create_generator_blueprint())
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
    "/agentic_generate_dagster",
    endpoint="agentic_generate_dagster",
    view_func=agentic_generate_dagster,
    methods=["POST", "OPTIONS"],
)
app.add_url_rule(
    "/agentic_generate_deployment_bundle",
    endpoint="agentic_generate_deployment_bundle",
    view_func=agentic_generate_deployment_bundle,
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


def _validation_status(report: Any) -> str:
    if isinstance(report, dict):
        return str(report.get("status") or "").strip().lower()
    return ""


def _invalid_codegen_nodes(generated_nodes: list[Any]) -> list[dict[str, Any]]:
    invalid_nodes = []
    for item in generated_nodes:
        if not isinstance(item, dict):
            continue
        artifact = item.get("generated_artifact")
        if not isinstance(artifact, dict):
            continue
        report = artifact.get("validation_report")
        if _validation_status(report) != "valid":
            invalid_nodes.append(
                {
                    "flow_id": str(item.get("flow_id") or ""),
                    "validation_report": report if isinstance(report, dict) else {},
                    "data_contract": artifact.get("data_contract")
                    if isinstance(artifact.get("data_contract"), dict)
                    else {},
                }
            )
    return invalid_nodes


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


def _node_data(node: dict[str, Any]) -> dict[str, Any]:
    data = node.get("data")
    return data if isinstance(data, dict) else node


def _node_file_entries(
    node: dict[str, Any],
    *,
    include_samples: bool = False,
    include_runtime_artifacts: bool = False,
) -> list[dict[str, Any]]:
    data = _node_data(node)
    raw_files = (
        data.get("file_buckets")
        if isinstance(data.get("file_buckets"), list)
        else data.get("files")
    )
    if not isinstance(raw_files, list):
        return []
    flow_id = str(node.get("id") or data.get("flow_id") or data.get("id") or "").strip()
    default_bucket = f"files-step-id-{flow_id}".lower()
    entries = []
    for item in raw_files:
        if isinstance(item, str):
            filename = item.strip()
            bucket = default_bucket
            content_type = None
        elif isinstance(item, dict):
            filename = str(item.get("filename") or item.get("name") or "").strip()
            bucket = str(item.get("bucket") or default_bucket).strip().lower()
            content_type = item.get("content_type")
        else:
            continue
        if not filename:
            continue
        if not include_runtime_artifacts and _is_codegen_runtime_file(filename):
            continue
        entry = {
            "filename": filename,
            "bucket": bucket,
            "content_type": content_type,
            **_file_kind_and_format(filename),
        }
        if include_samples:
            entry.update(_sample_file_descriptor(bucket, filename, entry))
        entries.append(entry)
    return entries


def _is_codegen_runtime_file(filename: str) -> bool:
    normalized = str(filename or "").strip()
    return normalized in CODEGEN_RUNTIME_FILENAMES or normalized.startswith("Dockerfile.")


def _sample_file_descriptor(
    bucket: str,
    filename: str,
    descriptor: dict[str, Any],
) -> dict[str, Any]:
    kind = descriptor.get("kind")
    file_format = descriptor.get("format")
    if kind not in {"table", "json", "text"}:
        return {}
    bucket_id = bucket.removeprefix("files-step-id-")
    try:
        response = _proxy(
            dispatch_object_request,
            "minio_read_file",
            method="GET",
            params={"bucket_id": bucket_id, "filename": filename},
            data=b"",
        )
        response.raise_for_status()
    except Exception:
        return {}
    content = response.content
    max_chars = 12000
    text = content[:max_chars].decode("utf-8", errors="replace")
    omitted = max(0, len(content) - len(content[:max_chars]))
    if kind == "table" and file_format in {"csv", "tsv"}:
        delimiter = "\t" if file_format == "tsv" else ","
        try:
            reader = csv.DictReader(StringIO(text), delimiter=delimiter)
            rows = [row for _, row in zip(range(10), reader)]
            columns = list(reader.fieldnames or [])
        except Exception:
            return {"sample": {"text": text, "omitted_bytes": omitted}}
        return {
            "columns": columns,
            "sample": {
                "rows": rows,
                "omitted_bytes": omitted,
            },
            "size_bytes": len(content),
        }
    if kind == "json":
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        rows = []
        columns = []
        if isinstance(parsed, list):
            rows = [item for item in parsed[:10] if isinstance(item, dict)]
        elif isinstance(parsed, dict):
            candidate_rows = parsed.get("records") or parsed.get("items")
            if isinstance(candidate_rows, list):
                rows = [item for item in candidate_rows[:10] if isinstance(item, dict)]
        if rows:
            columns = sorted({key for row in rows for key in row})
        return {
            **({"columns": columns} if columns else {}),
            "sample": {
                **({"rows": rows} if rows else {"text": text}),
                "omitted_bytes": omitted,
            },
            "size_bytes": len(content),
        }
    return {
        "sample": {"text": text, "omitted_bytes": omitted},
        "size_bytes": len(content),
    }


def _file_kind_and_format(filename: str) -> dict[str, str]:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in {"csv", "tsv", "parquet", "xlsx"}:
        return {"kind": "table", "format": extension}
    if extension == "json":
        return {"kind": "json", "format": "json"}
    if extension in {"txt", "md", "xml", "yaml", "yml"}:
        return {"kind": "text", "format": extension}
    if extension in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}:
        return {"kind": "image", "format": extension}
    return {"kind": "binary", "format": extension or "binary"}


def _node_descriptor(
    node: dict[str, Any],
    *,
    include_samples: bool = False,
) -> dict[str, Any]:
    data = _node_data(node)
    parameters = (
        dict(data.get("param"))
        if isinstance(data.get("param"), dict)
        else dict(data.get("implementation"))
        if isinstance(data.get("implementation"), dict)
        else {}
    )
    content = data.get("content")
    if isinstance(content, str) and content.strip():
        parameters["content"] = content.strip()
    return {
        "flow_id": str(node.get("id") or data.get("flow_id") or data.get("id") or ""),
        "label": str(data.get("label") or ""),
        "description": str(data.get("description") or ""),
        "type": str(data.get("type") or "custom"),
        "parameters": parameters,
        "files": _node_file_entries(node, include_samples=include_samples),
    }


def _build_codegen_context(
    graph: dict[str, Any],
    node_id: str,
    *,
    include_samples: bool = False,
) -> dict[str, Any] | None:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    target = next((node for node in nodes if str(node.get("id") or "") == node_id), None)
    if target is None:
        return None

    upstream_ids = [
        str(edge.get("source"))
        for edge in edges
        if isinstance(edge, dict) and str(edge.get("target") or "") == node_id
    ]
    downstream_ids = [
        str(edge.get("target"))
        for edge in edges
        if isinstance(edge, dict) and str(edge.get("source") or "") == node_id
    ]
    descriptors = [
        _node_descriptor(node, include_samples=include_samples)
        for node in nodes
        if isinstance(node, dict)
    ]
    node_by_id = {
        descriptor["flow_id"]: descriptor
        for descriptor in descriptors
        if descriptor.get("flow_id")
    }
    available_inputs = []
    for upstream_id in upstream_ids:
        available_inputs.extend(node_by_id.get(upstream_id, {}).get("files") or [])
    if not available_inputs:
        available_inputs = _node_file_entries(target, include_samples=include_samples)

    target_descriptor = _node_descriptor(target)
    output_name = (
        "".join(
            char.lower() if char.isalnum() else "_"
            for char in (target_descriptor["label"] or node_id)
        ).strip("_")
        or "output"
    )
    return {
        "schema_version": "inlumen.script-generation-context@1",
        "target_node": target_descriptor,
        "pipeline": graph.get("pipeline") if isinstance(graph.get("pipeline"), dict) else {},
        "graph": {
            "nodes": descriptors,
            "edges": [
                {"source": str(edge.get("source")), "target": str(edge.get("target"))}
                for edge in edges
                if isinstance(edge, dict) and edge.get("source") and edge.get("target")
            ],
            "upstream_nodes": upstream_ids,
            "downstream_nodes": downstream_ids,
        },
        "available_inputs": available_inputs,
        "expected_outputs": [
            {
                "name": output_name,
                "kind": "json",
                "format": "json",
                "description": "Generated node output.",
            }
        ],
        "runtime_constraints": {
            "language": "python",
            "python_version": "3.11",
            "base_image": "python:3.11-slim",
            "allowed_packages": [
                "pandas",
                "numpy",
                "pillow",
                "scikit-learn",
                "requests",
            ],
            "network_allowed": False,
            "max_runtime_seconds": 60,
        },
    }


def _post_codegen_request(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/node-script",
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(http_request, timeout=CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS) as response:
            response_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Codegen service rejected request: {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Codegen service unavailable at {CODEGEN_SERVICE_URL}: {exc}") from exc
    parsed = json.loads(response_payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Codegen service returned an invalid response")
    return parsed


def _post_codegen_pipeline_request(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/pipeline-scripts",
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(
            http_request,
            timeout=CODEGEN_PIPELINE_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            response_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Codegen service rejected request: {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Codegen service unavailable at {CODEGEN_SERVICE_URL}: {exc}") from exc
    parsed = json.loads(response_payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Codegen service returned an invalid response")
    return parsed


def _post_codegen_pipeline_run_request(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/pipeline-scripts/runs",
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(http_request, timeout=CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS) as response:
            response_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Codegen service rejected request: {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Codegen service unavailable at {CODEGEN_SERVICE_URL}: {exc}") from exc
    parsed = json.loads(response_payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Codegen service returned an invalid response")
    return parsed


def _post_codegen_pipeline_run_resume_request(
    run_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/pipeline-scripts/runs/{run_id}/resume",
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(http_request, timeout=CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS) as response:
            response_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Codegen service rejected request: {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Codegen service unavailable at {CODEGEN_SERVICE_URL}: {exc}") from exc
    parsed = json.loads(response_payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Codegen service returned an invalid response")
    return parsed


def _get_codegen_pipeline_run_request(run_id: str) -> dict[str, Any]:
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/pipeline-scripts/runs/{run_id}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(http_request, timeout=CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS) as response:
            response_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Codegen service rejected request: {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Codegen service unavailable at {CODEGEN_SERVICE_URL}: {exc}") from exc
    parsed = json.loads(response_payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Codegen service returned an invalid response")
    return parsed


def _codegen_llm_config_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_config = (
        payload.get("llm_config")
        if isinstance(payload.get("llm_config"), dict)
        else {}
    )
    env_config = {
        "model": os.getenv("INLUMEN_CODEGEN_LLM_MODEL", "").strip(),
        "base_url": os.getenv("INLUMEN_CODEGEN_LLM_BASE_URL", "").strip(),
        "api_key": os.getenv("INLUMEN_CODEGEN_LLM_API_KEY", "").strip(),
        "temperature": os.getenv("INLUMEN_CODEGEN_LLM_TEMPERATURE", "").strip(),
        "timeout_seconds": os.getenv("INLUMEN_CODEGEN_LLM_TIMEOUT_SECONDS", "").strip(),
    }
    merged = {
        key: value
        for key, value in {
            "model": raw_config.get("model") or env_config["model"],
            "base_url": raw_config.get("base_url")
            or raw_config.get("baseUrl")
            or env_config["base_url"],
            "api_key": raw_config.get("api_key")
            or raw_config.get("apiKey")
            or env_config["api_key"],
            "temperature": raw_config.get("temperature") or env_config["temperature"],
            "timeout_seconds": raw_config.get("timeout_seconds")
            or env_config["timeout_seconds"],
        }.items()
        if value not in (None, "")
    }
    return merged


def _codegen_configuration_hash(
    graph: dict[str, Any],
    node_id: str,
    artifact: dict[str, Any],
) -> str:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    node = next((item for item in nodes if str(item.get("id") or "") == node_id), None)
    data = _node_data(node) if isinstance(node, dict) else {}
    definition_id = str(data.get("definition_id") or "").strip()
    if not definition_id:
        return ""
    try:
        definition_version = int(data.get("definition_version") or 1)
    except (TypeError, ValueError):
        definition_version = 1
    implementation = data.get("implementation")
    if not isinstance(implementation, dict):
        implementation = {}
    contract = artifact.get("data_contract")
    contract_version = (
        str(contract.get("version") or "")
        if isinstance(contract, dict)
        else ""
    )
    return configuration_hash(
        definition_id=definition_id,
        definition_version=max(definition_version, 1),
        implementation=implementation,
        generator=str(artifact.get("generator") or "inlumen-codegen-service"),
        generator_version=str(artifact.get("generator_version") or ""),
        contract_version=contract_version,
    )


def _persist_codegen_artifact(
    node_id: str,
    artifact: dict[str, Any],
    graph: dict[str, Any],
) -> dict[str, Any]:
    files = artifact.get("files") if isinstance(artifact.get("files"), list) else []
    stored_files = []
    bucket = f"files-step-id-{node_id}".lower()
    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        filename = str(file_item.get("filename") or "").strip()
        content = file_item.get("content")
        if not filename or not isinstance(content, str):
            continue
        storage_response = _proxy(
            dispatch_object_request,
            "minio_update_text_file",
            method="PUT",
            data=b"",
            json_payload={
                "bucket_id": node_id,
                "filename": filename,
                "content": content,
            },
        )
        storage_response.raise_for_status()
        graph_response = _proxy(
            dispatch_graph_request,
            "neo4j_add_file",
            method="POST",
            data=b"",
            json_payload={"properties": {"flow_id": node_id, "filename": filename}},
        )
        graph_response.raise_for_status()
        stored_files.append(
            {
                "filename": filename,
                "bucket": bucket,
                "content_type": str(file_item.get("content_type") or "text/plain"),
            }
        )

    generated_artifact = {
        **artifact,
        "status": "current",
        "configuration_hash": artifact.get("configuration_hash")
        or _codegen_configuration_hash(graph, node_id, artifact),
        "files": stored_files,
    }
    graph_response = _proxy(
        dispatch_graph_request,
        "neo4j_update_generated_artifact",
        method="POST",
        data=b"",
        json_payload={
            "flow_id": node_id,
            "generated_artifact": generated_artifact,
        },
    )
    graph_response.raise_for_status()
    return generated_artifact


def _persist_codegen_run_report(run_report: Any) -> dict[str, Any] | None:
    if not isinstance(run_report, dict):
        return None
    run_id = str(run_report.get("run_id") or "").strip()
    if not run_id:
        return run_report
    filename = f"{run_id}.json"
    bucket_id = "pipeline-codegen-runs"
    bucket = f"files-step-id-{bucket_id}".lower()
    report = {
        **run_report,
        "object_storage": {
            "bucket": bucket,
            "filename": filename,
        },
    }
    try:
        storage_response = _proxy(
            dispatch_object_request,
            "minio_update_text_file",
            method="PUT",
            data=b"",
            json_payload={
                "bucket_id": bucket_id,
                "filename": filename,
                "content": json.dumps(report, indent=2, sort_keys=True) + "\n",
            },
        )
        storage_response.raise_for_status()
    except Exception as exc:
        warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
        warnings.append(f"Failed to persist generation run report: {exc}")
        report["warnings"] = warnings
    return report


def _build_pipeline_codegen_context(
    graph: dict[str, Any],
    *,
    include_samples: bool,
) -> dict[str, Any]:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    descriptors = [
        _node_descriptor(node, include_samples=include_samples)
        for node in nodes
        if isinstance(node, dict)
    ]
    return {
        "schema_version": "inlumen.pipeline-script-generation-context@1",
        "pipeline": graph.get("pipeline") if isinstance(graph.get("pipeline"), dict) else {},
        "graph": {
            "nodes": descriptors,
            "edges": [
                {"source": str(edge.get("source")), "target": str(edge.get("target"))}
                for edge in edges
                if isinstance(edge, dict) and edge.get("source") and edge.get("target")
            ],
        },
        "runtime_constraints": {
            "language": "python",
            "python_version": "3.11",
            "base_image": "python:3.11-slim",
            "allowed_packages": [
                "pandas",
                "numpy",
                "pillow",
                "scikit-learn",
                "requests",
            ],
            "network_allowed": False,
            "max_runtime_seconds": 60,
        },
    }


def _pipeline_sample_data_summary(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    sample_nodes = []
    sample_count = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        entries = _node_file_entries(node, include_runtime_artifacts=False)
        if not entries:
            continue
        data = _node_data(node)
        flow_id = str(node.get("id") or data.get("flow_id") or data.get("id") or "")
        sample_count += len(entries)
        sample_nodes.append(
            {
                "flow_id": flow_id,
                "label": str(data.get("label") or ""),
                "type": str(data.get("type") or "custom"),
                "files": [
                    {
                        "filename": str(item.get("filename") or ""),
                        "kind": str(item.get("kind") or ""),
                        "format": str(item.get("format") or ""),
                    }
                    for item in entries
                ],
            }
        )
    return {
        "has_sample_data": sample_count > 0,
        "sample_file_count": sample_count,
        "sample_nodes": sample_nodes,
    }


def _build_pipeline_codegen_payload(
    graph: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    include_sample_data = payload.get("include_sample_data", True) is not False
    validation_mode = str(payload.get("validation_mode") or "pipeline_sample").strip()
    if validation_mode not in {"static", "unit", "edge", "pipeline_sample"}:
        validation_mode = "pipeline_sample"
    repair_attempts = payload.get("repair_attempts", 2)
    try:
        repair_attempts = max(0, int(repair_attempts))
    except (TypeError, ValueError):
        repair_attempts = 2
    options = {
        "persist": False,
        "repair_attempts": repair_attempts,
        "include_sample_data": include_sample_data,
        "validation_mode": validation_mode,
        "allow_deterministic_fallback": bool(payload.get("allow_deterministic_fallback")),
        "user_instruction": str(payload.get("user_instruction") or ""),
    }
    metadata = {
        "generation_mode": str(payload.get("generation_mode") or "").strip(),
        "data_awareness": _pipeline_sample_data_summary(graph),
        "options": options,
    }
    return (
        {
            "context": _build_pipeline_codegen_context(
                graph,
                include_samples=include_sample_data,
            ),
            "options": options,
            "llm_config": _codegen_llm_config_from_payload(payload),
        },
        metadata,
    )


def _finalize_pipeline_codegen_response(
    codegen_response: dict[str, Any],
    graph: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    generated_nodes = (
        codegen_response.get("nodes")
        if isinstance(codegen_response.get("nodes"), list)
        else []
    )
    integration_validation = (
        codegen_response.get("integration_validation")
        if isinstance(codegen_response.get("integration_validation"), dict)
        else {}
    )
    generation_run = _persist_codegen_run_report(codegen_response.get("generation_run"))
    invalid_nodes = _invalid_codegen_nodes(generated_nodes)
    if _validation_status(integration_validation) != "valid" or invalid_nodes:
        return (
            False,
            {
                "nodes": generated_nodes,
                "edges": codegen_response.get("edges", []),
                "invalid_nodes": invalid_nodes,
                "integration_validation": integration_validation,
                "generation_run": generation_run,
                "codegen_service_url": CODEGEN_SERVICE_URL,
            },
        )

    persisted_nodes = []
    for item in generated_nodes:
        if not isinstance(item, dict):
            continue
        flow_id = str(item.get("flow_id") or "").strip()
        artifact = item.get("generated_artifact")
        if not flow_id or not isinstance(artifact, dict):
            continue
        persisted_nodes.append(
            {
                "flow_id": flow_id,
                "generated_artifact": _persist_codegen_artifact(
                    flow_id,
                    artifact,
                    graph,
                ),
            }
        )
    return (
        True,
        {
            "nodes": persisted_nodes,
            "edges": codegen_response.get("edges", []),
            "integration_validation": integration_validation,
            "generation_run": generation_run,
            "codegen_service_url": CODEGEN_SERVICE_URL,
        },
    )


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
        "codegenModel": str(config.get("codegenModel") or config.get("codegen_model") or ""),
        "codegen_model": str(config.get("codegenModel") or config.get("codegen_model") or ""),
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
    codegen_model = str(
        payload.get("codegenModel")
        or payload.get("codegen_model")
        or existing.get("codegenModel")
        or existing.get("codegen_model")
        or ""
    ).strip()
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
        "codegenModel": codegen_model,
        "codegen_model": codegen_model,
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


@app.route("/api/pipeline/graph", methods=["GET", "POST", "OPTIONS"])
@require_auth
def pipeline_graph():
    if request.method == "POST":
        return _proxy_response(dispatch_graph_request, "neo4j_sync_graph")
    return _proxy_response(dispatch_graph_request, "neo4j_get_graph")


@app.route("/api/pipeline/updated-at", methods=["GET", "OPTIONS"])
@require_auth
def pipeline_updated_at():
    return _proxy_response(dispatch_graph_request, "neo4j_get_pipeline_updated_at")


@app.route("/api/pipeline/history/restore", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_history_restore():
    return _proxy_response(dispatch_graph_request, "neo4j_restore_graph_history")


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


@app.route("/api/workspace/clear-all", methods=["POST", "OPTIONS"])
@require_auth
def workspace_clear_all():
    if request.method == "OPTIONS":
        return _preflight_response()

    payload = _request_json()
    session_id = str(payload.get("session_id") or "").strip()
    graph_response = _proxy(
        dispatch_graph_request,
        "neo4j_clear_pipeline_workspace",
        method="POST",
        json_payload={},
    )
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

    chat_reset = False
    if session_id:
        clear_state_from_disk(session_id)
        chat_reset = True

    if isinstance(graph_payload, dict):
        graph_payload["storage_cleanup"] = storage_cleanup
        graph_payload["chat_reset"] = chat_reset
        return jsonify(graph_payload), graph_response.status_code
    return jsonify({
        "graph": graph_payload,
        "storage_cleanup": storage_cleanup,
        "chat_reset": chat_reset,
    }), graph_response.status_code


@app.route("/api/provenance/report", methods=["GET", "OPTIONS"])
@require_auth
def provenance_report():
    if request.method == "OPTIONS":
        return _preflight_response()

    version_uid = str(request.args.get("version_uid") or "").strip()
    params = {"version_uid": version_uid} if version_uid else {}
    graph_response = _proxy(
        dispatch_graph_request,
        "neo4j_get_provenance_events",
        method="GET",
        params=params,
        data=b"",
    )
    if not graph_response.ok:
        return _response_from_upstream(graph_response)

    payload = _upstream_json(graph_response)
    if not isinstance(payload, dict):
        payload = {"events": []}
    version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
    pdf_bytes = build_provenance_pdf(payload)
    filename = provenance_report_filename(version.get("name"), version.get("uid"))
    response = Response(pdf_bytes, status=200, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/provenance/prov-o", methods=["GET", "OPTIONS"])
@require_auth
def provenance_prov_o():
    if request.method == "OPTIONS":
        return _preflight_response()

    version_uid = str(request.args.get("version_uid") or "").strip()
    params = {"version_uid": version_uid} if version_uid else {}
    graph_response = _proxy(
        dispatch_graph_request,
        "neo4j_get_provenance_events",
        method="GET",
        params=params,
        data=b"",
    )
    if not graph_response.ok:
        return _response_from_upstream(graph_response)

    payload = _upstream_json(graph_response)
    if not isinstance(payload, dict):
        payload = {"events": []}
    version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
    document = build_prov_o_jsonld(payload)
    filename = provenance_prov_o_filename(version.get("name"), version.get("uid"))
    response = Response(
        json.dumps(document, ensure_ascii=False, indent=2),
        status=200,
        mimetype="application/ld+json",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response


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


@app.route("/api/nodes/<node_id>/generate-script", methods=["POST", "OPTIONS"])
@require_auth
def node_generate_script(node_id: str):
    if request.method == "OPTIONS":
        return _preflight_response()
    payload = _request_json()
    try:
        graph_response = _proxy(
            dispatch_graph_request,
            "neo4j_get_graph",
            method="GET",
            data=b"",
        )
        graph_response.raise_for_status()
        graph = _upstream_json(graph_response)
        graph = graph if isinstance(graph, dict) else {}
        context = _build_codegen_context(
            graph,
            node_id,
            include_samples=bool(payload.get("include_sample_data")),
        )
        if context is None:
            return _json_error(404, "node not found")

        codegen_payload = {
            "context": context,
            "options": {
                "persist": False,
                "repair_attempts": 2,
                "include_sample_data": bool(payload.get("include_sample_data")),
                "user_instruction": str(payload.get("user_instruction") or ""),
            },
            "llm_config": _codegen_llm_config_from_payload(payload),
        }
        codegen_response = _post_codegen_request(codegen_payload)
        artifact = codegen_response.get("generated_artifact")
        if not isinstance(artifact, dict):
            return _json_error(502, "codegen response did not include generated_artifact")
        validation_report = (
            artifact.get("validation_report")
            if isinstance(artifact.get("validation_report"), dict)
            else {}
        )
        if _validation_status(validation_report) != "valid":
            return _json_error(
                422,
                "script generation failed validation; no artifact was persisted",
                {
                    "flow_id": node_id,
                    "generated_artifact": artifact,
                    "validation_report": validation_report,
                    "codegen_service_url": CODEGEN_SERVICE_URL,
                },
            )
        generated_artifact = _persist_codegen_artifact(node_id, artifact, graph)
        return jsonify(
            {
                "flow_id": node_id,
                "generated_artifact": generated_artifact,
                "files": generated_artifact.get("files", []),
                "validation_report": generated_artifact.get("validation_report", {}),
                "codegen_service_url": CODEGEN_SERVICE_URL,
            }
        ), 200
    except Exception as exc:
        return _json_error(502, "script generation failed", str(exc))


@app.route("/api/pipeline/generate-scripts", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_generate_scripts():
    if request.method == "OPTIONS":
        return _preflight_response()
    payload = _request_json()
    try:
        graph_response = _proxy(
            dispatch_graph_request,
            "neo4j_get_graph",
            method="GET",
            data=b"",
        )
        graph_response.raise_for_status()
        graph = _upstream_json(graph_response)
        graph = graph if isinstance(graph, dict) else {}
        codegen_context = _build_pipeline_codegen_context(
            graph,
            include_samples=payload.get("include_sample_data", True) is not False,
        )
        if not codegen_context["graph"]["nodes"]:
            return _json_error(422, "pipeline graph has no nodes")

        codegen_payload, _metadata = _build_pipeline_codegen_payload(graph, payload)
        codegen_response = _post_codegen_pipeline_request(codegen_payload)
        is_valid, response_payload = _finalize_pipeline_codegen_response(
            codegen_response,
            graph,
        )
        if not is_valid:
            return _json_error(
                422,
                "pipeline script generation failed validation; no artifacts were persisted",
                response_payload,
            )
        return jsonify(response_payload), 200
    except Exception as exc:
        return _json_error(502, "pipeline script generation failed", str(exc))


@app.route("/api/pipeline/generation-runs", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_generation_runs():
    if request.method == "OPTIONS":
        return _preflight_response()
    payload = _request_json()
    try:
        graph_response = _proxy(
            dispatch_graph_request,
            "neo4j_get_graph",
            method="GET",
            data=b"",
        )
        graph_response.raise_for_status()
        graph = _upstream_json(graph_response)
        graph = graph if isinstance(graph, dict) else {}
        codegen_payload, metadata = _build_pipeline_codegen_payload(graph, payload)
        if not codegen_payload["context"]["graph"]["nodes"]:
            return _json_error(422, "pipeline graph has no nodes")

        codegen_run = _post_codegen_pipeline_run_request(codegen_payload)
        run_id = str(codegen_run.get("run_id") or "").strip()
        if not run_id:
            return _json_error(502, "codegen service did not return a run_id")
        PIPELINE_GENERATION_RUNS[run_id] = {
            "graph": graph,
            "metadata": metadata,
            "persisted": False,
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
        }
        return jsonify(
            {
                **codegen_run,
                "mode": metadata["generation_mode"],
                "data_awareness": metadata["data_awareness"],
                "persistence": {"status": "pending"},
            }
        ), 202
    except Exception as exc:
        return _json_error(502, "pipeline script generation failed", str(exc))


@app.route("/api/pipeline/generation-runs/<run_id>/resume", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_generation_run_resume(run_id: str):
    if request.method == "OPTIONS":
        return _preflight_response()
    payload = _request_json()
    try:
        local_run = PIPELINE_GENERATION_RUNS.get(run_id)
        if local_run is None:
            return _json_error(
                404,
                "pipeline generation run cannot be resumed; backend run metadata is unavailable",
            )
        resume_payload = {
            "flow_id": str(payload.get("flow_id") or "").strip() or None,
            "repair_attempts": payload.get("repair_attempts", 4),
            "user_instruction": str(payload.get("user_instruction") or ""),
        }
        codegen_run = _post_codegen_pipeline_run_resume_request(
            run_id,
            resume_payload,
        )
        new_run_id = str(codegen_run.get("run_id") or "").strip()
        if not new_run_id:
            return _json_error(502, "codegen service did not return a resumed run_id")

        metadata = dict(local_run.get("metadata") or {})
        options = dict(metadata.get("options") or {})
        try:
            options["repair_attempts"] = max(
                int(options.get("repair_attempts") or 0),
                int(resume_payload["repair_attempts"] or 4),
            )
        except (TypeError, ValueError):
            options["repair_attempts"] = 4
        metadata["options"] = options
        metadata["generation_mode"] = "repair"
        PIPELINE_GENERATION_RUNS[new_run_id] = {
            "graph": local_run["graph"],
            "metadata": metadata,
            "persisted": False,
            "resumed_from_run_id": run_id,
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
        }
        return jsonify(
            {
                **codegen_run,
                "mode": metadata["generation_mode"],
                "data_awareness": metadata.get("data_awareness", {}),
                "persistence": {"status": "pending"},
            }
        ), 202
    except Exception as exc:
        return _json_error(502, "pipeline generation run resume failed", str(exc))


@app.route("/api/pipeline/generation-runs/<run_id>", methods=["GET", "OPTIONS"])
@require_auth
def pipeline_generation_run(run_id: str):
    if request.method == "OPTIONS":
        return _preflight_response()
    try:
        codegen_run = _get_codegen_pipeline_run_request(run_id)
        local_run = PIPELINE_GENERATION_RUNS.get(run_id)
        response_payload = {
            **codegen_run,
            "mode": (local_run or {}).get("metadata", {}).get("generation_mode", ""),
            "data_awareness": (local_run or {}).get("metadata", {}).get(
                "data_awareness",
                {},
            ),
            "persistence": {"status": "pending"},
        }
        status = str(codegen_run.get("status") or "").strip().lower()
        result = codegen_run.get("result") if isinstance(codegen_run.get("result"), dict) else None
        if result is None or status not in {"valid", "invalid", "failed"}:
            return jsonify(response_payload), 200

        if local_run is None:
            response_payload["persistence"] = {
                "status": "not_available",
                "warning": "Backend run metadata is no longer available; artifacts were not persisted.",
            }
            return jsonify(response_payload), 200

        if local_run.get("persisted_response"):
            persistence_status = (
                "persisted" if local_run.get("persisted") else "not_persisted"
            )
            response_payload["persistence"] = {
                "status": persistence_status,
                "result": local_run["persisted_response"],
            }
            if persistence_status == "not_persisted":
                response_payload["persistence"][
                    "reason"
                ] = "pipeline script generation failed validation"
            return jsonify(response_payload), 200

        is_valid, finalized = _finalize_pipeline_codegen_response(
            result,
            local_run["graph"],
        )
        local_run["updated_at"] = _utc_now_iso()
        if is_valid:
            local_run["persisted"] = True
            local_run["persisted_response"] = finalized
            response_payload["persistence"] = {
                "status": "persisted",
                "result": finalized,
            }
        else:
            local_run["persisted"] = False
            local_run["persisted_response"] = finalized
            response_payload["persistence"] = {
                "status": "not_persisted",
                "reason": "pipeline script generation failed validation",
                "result": finalized,
            }
        return jsonify(response_payload), 200
    except Exception as exc:
        return _json_error(502, "pipeline generation run lookup failed", str(exc))


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
    if storage_response.ok:
        _proxy(
            dispatch_graph_request,
            "neo4j_record_provenance_event",
            method="POST",
            json_payload={
                "actor": "manual",
                "action": "file_updated",
                "summary": f"Updated text file '{filename}' for step {node_id}.",
                "details": {
                    "flow_id": node_id,
                    "filename": filename,
                    "container_id": container_id,
                },
            },
        )
    return _response_from_upstream(storage_response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=INLUMEN_API_PORT)
