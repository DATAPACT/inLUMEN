import base64
import csv
import hashlib
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, make_response, request

from artifact_contract import classify_artifact
from analytics_api import (
    agentic_generate_deployment_bundle,
    agentic_generate_dagster,
    agentic_generate_dockerfiles,
    agentic_generate_version_yamls,
    agentic_generate_yaml,
    agentic_pipeline_editor,
    agentic_pipeline_editor_reset,
)
from attachment_validation import attachment_input_errors, read_attachment_probe
from auth_middleware import require_auth
from chat_state import clear_state_from_disk
from codegen_runs import CodegenRunStore
from generators.routes import create_generator_blueprint
from graph_client import dispatch_graph_request
from local_api_client import LocalApiResponse
from node_definitions import create_node_definitions_blueprint
from node_definitions.artifacts import (
    configuration_definition_id,
    configuration_hash,
    implementation_plan_from_data,
)
from node_ports import normalize_node_ports
from object_client import dispatch_object_request
from provenance_provo import build_prov_o_jsonld, provenance_prov_o_filename
from provenance_report import build_provenance_pdf, provenance_report_filename
from public_api import create_public_api_blueprint
from runtime_config import add_cors_headers, get_service_port
from step_types import normalize_step_type


INLUMEN_API_PORT = get_service_port("INLUMEN_API_PORT", 5000)
CODEGEN_SERVICE_URL = os.getenv(
    "INLUMEN_CODEGEN_SERVICE_URL",
    "http://127.0.0.1:8010",
).rstrip("/")
CODEGEN_SERVICE_API_KEY = os.getenv("INLUMEN_CODEGEN_SERVICE_API_KEY", "").strip()
CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv("INLUMEN_CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS", "300")
)
CODEGEN_PIPELINE_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv("INLUMEN_CODEGEN_PIPELINE_REQUEST_TIMEOUT_SECONDS", "1200")
)
CODEGEN_SAMPLE_BINARY_MAX_BYTES = int(
    os.getenv("INLUMEN_CODEGEN_SAMPLE_BINARY_MAX_BYTES", str(16 * 1024 * 1024))
)
CODEGEN_RUN_STORE = CodegenRunStore(
    os.getenv("INLUMEN_CODEGEN_RUN_DB_PATH", "state/codegen-runs.sqlite3")
)
DEFAULT_CODEGEN_ALLOWED_PACKAGES = [
    "pandas",
    "numpy",
    "pillow",
    "scikit-learn",
    "requests",
    "pypdf",
    "SpeechRecognition",
    "pocketsphinx",
    "textblob",
]
CODEGEN_RUNTIME_FILENAMES = {
    "main.py",
    "requirements.txt",
    "node-manifest.json",
    "validation-report.json",
}
PIPELINE_RUNTIME_BEHAVIOR_INSTRUCTION = """Design the entire pipeline as one coherent program before writing any node.
For every edge, decide the exact output filename, format, schema or object shape, and required runtime dependencies. The producer must write that contract and the consumer must read the same contract.
Implement every capability requested by the high-level request and graph. A terminal behavior such as answering questions, alerting, or publishing results must produce that real result, not only an intermediate score, index, or status.
The user supplies input files containing real data. Never create input data, sample data, fake data, placeholder data, or fallback success artifacts.
Process every supplied input with a real parser, model, algorithm, or service implementation. Never mock, simulate, stub, fabricate, or approximate the requested transformation, and never treat a structured binary format as plain UTF-8 text.
Preserve information needed by downstream nodes. For example, retrieval pipelines must carry source chunks or records alongside vectors so the final node can return grounded content rather than only an embedding index or similarity score.
Every node must be a finite, non-interactive Python 3.11 batch program. Validate required inputs before loading large models or doing other slow setup. Invalid inputs and processing failures must raise a clear error and exit non-zero; never serialize an exception or placeholder as a successful output.
Before returning code, self-check every graph edge, filename, serialization shape, dependency, and requested terminal result end to end."""

PIPELINE_RUNTIME_ATTACHMENT_INSTRUCTION = f"""{PIPELINE_RUNTIME_BEHAVIOR_INSTRUCTION}

Create the files needed to run every pipeline node.
For each flow_id, return main.py and requirements.txt. Leave requirements.txt empty when the script only uses the Python 3.11 standard library.
When a node runs, its input files and files from the previous node are in the current working directory. Read them from there and write the node's output files there.
Keep connected nodes consistent about output filenames and formats.
Each main.py must be a finite, non-interactive batch program: do not call input(), start a server or UI, watch for files, or loop forever. Validate required input files before loading large models or doing other slow setup, then exit when the output files are written.
Associate each returned file with its flow_id so inLUMEN can attach it to the correct node."""
EXTERNAL_AI_RUNTIME_RESPONSE_INSTRUCTION = f"""{PIPELINE_RUNTIME_BEHAVIOR_INSTRUCTION}

For every node, create:
- main.py
- requirements.txt only if main.py needs third-party packages

Return code files only. Never create or return input data, example data, fake files, placeholder files, credentials, Dockerfiles, Dagster files, or manifests. The user will supply real input data separately from inLUMEN's Run tab.

Each main.py runs with the root node's Run-tab inputs or the previous node's output files in the current folder. It must read from that folder and write its results back to that folder. Connected nodes must agree on output filenames and formats.

Each main.py must be a one-shot, non-interactive Python 3.11 batch program. It must not call input(), start a server or UI, watch for files, sleep indefinitely, or loop forever. It must validate required input files before loading large models, downloading resources, or doing other slow setup; invalid inputs must fail immediately with a clear error. A node with a downstream connection must write at least one real output file, then exit successfully.

If you can create files, return one ZIP with folders named nodes/<flow_id>/. Otherwise, show each file in its own code block under a clear NODE <flow_id> heading. Return complete working code, not pseudocode.

Finish with a short RUN INPUT MAP. For every real external input the user must provide, state the exact filename, flow_id, and root node label that first reads it. The user supplies these ephemeral files in the Run tab; they must not be attached to nodes as runtime code or test fixtures."""
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


def _invalid_codegen_nodes(
    generated_nodes: list[Any],
    expected_flow_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    invalid_nodes: list[dict[str, Any]] = []
    seen_flow_ids: list[str] = []
    for item in generated_nodes:
        if not isinstance(item, dict):
            invalid_nodes.append(
                {
                    "flow_id": "",
                    "errors": ["Generated node result is not an object."],
                }
            )
            continue
        flow_id = str(item.get("flow_id") or "").strip()
        if not flow_id:
            invalid_nodes.append(
                {
                    "flow_id": "",
                    "errors": ["Generated node result is missing flow_id."],
                }
            )
            continue
        seen_flow_ids.append(flow_id)
        artifact = item.get("generated_artifact")
        if not isinstance(artifact, dict):
            invalid_nodes.append(
                {
                    "flow_id": flow_id,
                    "errors": ["Generated node result is missing generated_artifact."],
                }
            )
            continue
        errors: list[str] = []
        report = artifact.get("validation_report")
        if _validation_status(report) != "valid":
            errors.append("Generated node validation did not pass.")
        files = artifact.get("files") if isinstance(artifact.get("files"), list) else []
        has_python_script = any(
            isinstance(file_item, dict)
            and str(file_item.get("filename") or "").strip().lower().endswith(".py")
            and isinstance(file_item.get("content"), str)
            and bool(str(file_item.get("content") or "").strip())
            for file_item in files
        )
        if not has_python_script:
            errors.append("Generated node does not include a non-empty Python script.")
        if errors:
            invalid_nodes.append(
                {
                    "flow_id": flow_id,
                    "errors": errors,
                    "validation_report": report if isinstance(report, dict) else {},
                    "data_contract": artifact.get("data_contract")
                    if isinstance(artifact.get("data_contract"), dict)
                    else {},
                }
            )

    expected = [str(flow_id).strip() for flow_id in (expected_flow_ids or []) if str(flow_id).strip()]
    for flow_id in expected:
        count = seen_flow_ids.count(flow_id)
        if count == 0:
            invalid_nodes.append(
                {
                    "flow_id": flow_id,
                    "errors": ["No generated files were returned for this pipeline node."],
                }
            )
        elif count > 1:
            invalid_nodes.append(
                {
                    "flow_id": flow_id,
                    "errors": ["More than one generated result was returned for this pipeline node."],
                }
            )
    expected_set = set(expected)
    unknown_flow_ids = sorted(set(seen_flow_ids) - expected_set) if expected else []
    for flow_id in unknown_flow_ids:
        invalid_nodes.append(
            {
                "flow_id": flow_id,
                "errors": ["Generated files were returned for an unknown pipeline node."],
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


def _codegen_job_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep progress metadata while avoiding duplicate generated file payloads."""
    return {
        key: deepcopy(payload[key])
        for key in (
            "run_id",
            "status",
            "resumed_from_run_id",
            "resume_from_flow_id",
            "generation_run",
            "error",
            "created_at",
            "updated_at",
        )
        if key in payload
    }


def _codegen_context_fingerprint(context: dict[str, Any]) -> str:
    encoded = json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _codegen_run_summary(local_run: dict[str, Any]) -> dict[str, Any]:
    metadata = (
        local_run.get("metadata")
        if isinstance(local_run.get("metadata"), dict)
        else {}
    )
    remote_job = (
        local_run.get("remote_job")
        if isinstance(local_run.get("remote_job"), dict)
        else {}
    )
    if local_run.get("cancelled"):
        persistence = {"status": "cancelled"}
    elif local_run.get("persisted_response"):
        persistence = {
            "status": "persisted"
            if local_run.get("persisted")
            else "not_persisted"
        }
    else:
        persistence = {"status": "pending"}
    return {
        **remote_job,
        "run_id": str(local_run.get("run_id") or remote_job.get("run_id") or ""),
        "status": str(
            local_run.get("status") or remote_job.get("status") or "queued"
        ),
        "mode": metadata.get("generation_mode", ""),
        "data_awareness": metadata.get("data_awareness", {}),
        "persistence": persistence,
        "created_at": local_run.get("created_at"),
        "updated_at": local_run.get("updated_at"),
    }


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
    lower = normalized.lower()
    return (
        lower in CODEGEN_RUNTIME_FILENAMES
        or lower.startswith("dockerfile.")
        or lower.endswith((".py", ".pyi", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".tsx"))
    )


def _sample_file_descriptor(
    bucket: str,
    filename: str,
    descriptor: dict[str, Any],
) -> dict[str, Any]:
    kind = descriptor.get("kind")
    file_format = descriptor.get("format")
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
    if kind not in {"table", "json", "text"}:
        if len(content) > CODEGEN_SAMPLE_BINARY_MAX_BYTES:
            return {
                "size_bytes": len(content),
                "sample_transport_error": (
                    f"Real input exceeds the {CODEGEN_SAMPLE_BINARY_MAX_BYTES}-byte "
                    "binary validation transport limit."
                ),
            }
        return {
            "size_bytes": len(content),
            "sample": {
                "content_base64": base64.b64encode(content).decode("ascii"),
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "omitted_bytes": 0,
            },
        }
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
    return classify_artifact(filename)


def _node_descriptor(
    node: dict[str, Any],
    *,
    include_samples: bool = False,
) -> dict[str, Any]:
    data = _node_data(node)
    parameters = (
        dict(data.get("param"))
        if isinstance(data.get("param"), dict)
        else {}
    )
    content = data.get("content")
    if isinstance(content, str) and content.strip():
        parameters["content"] = content.strip()
    implementation = implementation_plan_from_data(data)
    subpipeline = data.get("subpipeline")
    if not isinstance(subpipeline, dict):
        try:
            subpipeline = json.loads(data.get("subpipeline_json") or "{}")
        except (TypeError, ValueError):
            subpipeline = {}
    return {
        "flow_id": str(node.get("id") or data.get("flow_id") or data.get("id") or ""),
        "label": str(data.get("label") or ""),
        "description": str(data.get("description") or ""),
        "type": normalize_step_type(data.get("type")),
        "template": str(data.get("template_label") or data.get("definition_id") or ""),
        "parameters": parameters,
        "implementation": implementation,
        "subpipeline": subpipeline if isinstance(subpipeline, dict) else {},
        "ports": normalize_node_ports(data.get("ports"), data.get("type")),
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
                {
                    "source": str(edge.get("source")),
                    "target": str(edge.get("target")),
                    "source_port": str(edge.get("sourceHandle") or edge.get("source_port") or ""),
                    "target_port": str(edge.get("targetHandle") or edge.get("target_port") or ""),
                }
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
            "allowed_packages": DEFAULT_CODEGEN_ALLOWED_PACKAGES,
            "allow_unlisted_model_packages": True,
            "network_allowed": True,
            "max_runtime_seconds": 900,
        },
    }


def _post_codegen_request(payload: dict[str, Any]) -> dict[str, Any]:
    encoded, headers = _codegen_request_parts(payload)
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/node-script",
        data=encoded,
        headers=headers,
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
    encoded, headers = _codegen_request_parts(payload)
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/pipeline-scripts",
        data=encoded,
        headers=headers,
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
    encoded, headers = _codegen_request_parts(payload)
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/pipeline-scripts/runs",
        data=encoded,
        headers=headers,
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
    encoded, headers = _codegen_request_parts(payload)
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/pipeline-scripts/runs/{run_id}/resume",
        data=encoded,
        headers=headers,
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
        headers=_codegen_request_headers(include_content_type=False),
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


def _cancel_codegen_pipeline_run_request(run_id: str) -> dict[str, Any]:
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/generate/pipeline-scripts/runs/{run_id}",
        headers=_codegen_request_headers(include_content_type=False),
        method="DELETE",
    )
    try:
        with urlopen(
            http_request,
            timeout=CODEGEN_NODE_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            response_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Codegen service rejected cancellation: {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Codegen service unavailable at {CODEGEN_SERVICE_URL}: {exc}"
        ) from exc
    parsed = json.loads(response_payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Codegen service returned an invalid cancellation response")
    return parsed


def _prepare_codegen_request(
    payload: dict[str, Any],
) -> dict[str, Any]:
    request_payload = deepcopy(payload)
    raw_config = request_payload.get("llm_config")
    if isinstance(raw_config, dict):
        raw_config.pop("api_key", None)
        raw_config.pop("apiKey", None)
    return request_payload


def _codegen_llm_api_key(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    config = payload.get("llm_config")
    if not isinstance(config, dict):
        return ""
    return str(config.get("api_key") or config.get("apiKey") or "").strip()


def _codegen_request_headers(
    *,
    include_content_type: bool = True,
    llm_api_key: str = "",
) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if include_content_type:
        headers["Content-Type"] = "application/json"

    service_api_key = (
        os.getenv("INLUMEN_CODEGEN_SERVICE_API_KEY", "").strip()
        or CODEGEN_SERVICE_API_KEY
    )
    if service_api_key:
        headers["Authorization"] = f"Bearer {service_api_key}"
    if llm_api_key:
        headers["X-LLM-API-Key"] = llm_api_key
    return headers


def _codegen_request_parts(
    payload: dict[str, Any] | None = None,
) -> tuple[bytes | None, dict[str, str]]:
    request_payload: dict[str, Any] | None = None
    if payload is not None:
        request_payload = _prepare_codegen_request(payload)
    headers = _codegen_request_headers(
        include_content_type=payload is not None,
        llm_api_key=_codegen_llm_api_key(payload),
    )
    encoded = (
        json.dumps(request_payload).encode("utf-8")
        if request_payload is not None
        else None
    )
    return encoded, headers


def _codegen_configuration_hash(
    graph: dict[str, Any],
    node_id: str,
    artifact: dict[str, Any],
) -> str:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    node = next((item for item in nodes if str(item.get("id") or "") == node_id), None)
    data = _node_data(node) if isinstance(node, dict) else {}
    definition_id = configuration_definition_id(data, flow_id=node_id)
    try:
        definition_version = int(data.get("definition_version") or 1)
    except (TypeError, ValueError):
        definition_version = 1
    implementation = implementation_plan_from_data(data)
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
    new_filenames = {
        str(item.get("filename") or "").strip()
        for item in files
        if isinstance(item, dict) and str(item.get("filename") or "").strip()
    }
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    current_node = next(
        (
            item
            for item in nodes
            if isinstance(item, dict)
            and str(item.get("id") or item.get("flow_id") or "") == node_id
        ),
        None,
    )
    current_data = _node_data(current_node) if isinstance(current_node, dict) else {}
    current_artifact = (
        current_data.get("generated_artifact")
        if isinstance(current_data.get("generated_artifact"), dict)
        else {}
    )
    stale_filenames = {
        str(item.get("filename") or "").strip()
        for item in current_artifact.get("files") or []
        if isinstance(item, dict)
        and str(item.get("filename") or "").strip()
        and str(item.get("filename") or "").strip() not in new_filenames
    }
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
            json_payload={
                "properties": {
                    "flow_id": node_id,
                    "filename": filename,
                    "role": "code",
                }
            },
        )
        graph_response.raise_for_status()
        stored_files.append(
            {
                "filename": filename,
                "bucket": bucket,
                "content_type": str(file_item.get("content_type") or "text/plain"),
                "role": "code",
            }
        )

    for filename in sorted(stale_filenames):
        storage_response = _proxy(
            dispatch_object_request,
            "minio_remove_file",
            method="DELETE",
            params={},
            data=b"",
            form={"bucket_id": node_id, "filename": filename},
        )
        storage_response.raise_for_status()
        graph_response = _proxy(
            dispatch_graph_request,
            "neo4j_delete_file",
            method="DELETE",
            params={},
            data=b"",
            json_payload={
                "properties": {"flow_id": node_id, "filename": filename}
            },
        )
        graph_response.raise_for_status()

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
                {
                    "source": str(edge.get("source")),
                    "target": str(edge.get("target")),
                    "source_port": str(
                        edge.get("sourceHandle") or edge.get("source_port") or ""
                    ),
                    "target_port": str(
                        edge.get("targetHandle") or edge.get("target_port") or ""
                    ),
                }
                for edge in edges
                if isinstance(edge, dict) and edge.get("source") and edge.get("target")
            ],
        },
        "runtime_constraints": {
            "language": "python",
            "python_version": "3.11",
            "base_image": "python:3.11-slim",
            "allowed_packages": DEFAULT_CODEGEN_ALLOWED_PACKAGES,
            "allow_unlisted_model_packages": True,
            "network_allowed": True,
            "max_runtime_seconds": 900,
        },
    }


def _build_external_ai_runtime_prompt(
    graph: dict[str, Any],
    high_level_prompt: str = "",
) -> str:
    context = _build_pipeline_codegen_context(graph, include_samples=False)
    graph_context = context.get("graph") if isinstance(context.get("graph"), dict) else {}
    pipeline_request = str(high_level_prompt or "").strip()
    if not pipeline_request:
        pipeline_request = (
            "Infer the intended pipeline behavior from the node labels, descriptions, "
            "parameters, attachments, and edges below."
        )
    return (
        "You are preparing runtime files that a user will manually upload to the "
        "matching nodes of an inLUMEN pipeline.\n\n"
        "HIGH-LEVEL PIPELINE REQUEST:\n"
        f"{pipeline_request}\n\n"
        "RUNTIME AND DELIVERY CONTRACT:\n"
        f"{EXTERNAL_AI_RUNTIME_RESPONSE_INSTRUCTION}\n\n"
        "PIPELINE GRAPH:\n"
        f"{json.dumps(graph_context, indent=2, sort_keys=True)}\n"
    )


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
                "type": normalize_step_type(data.get("type")),
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
    repair_attempts = payload.get("repair_attempts", 7)
    try:
        repair_attempts = max(0, int(repair_attempts))
    except (TypeError, ValueError):
        repair_attempts = 7
    requested_generation_strategy = str(
        payload.get("generation_strategy") or "auto"
    ).strip()
    generation_strategy = {
        "auto": "pipeline_first",
        "single_pass": "pipeline_first",
        "per_node": "node_first",
        "pipeline_first": "pipeline_first",
        "node_first": "node_first",
    }.get(requested_generation_strategy, "pipeline_first")
    high_level_prompt = str(
        payload.get("high_level_prompt")
        or payload.get("user_instruction")
        or ""
    ).strip()
    user_instruction = PIPELINE_RUNTIME_ATTACHMENT_INSTRUCTION
    if high_level_prompt:
        user_instruction += (
            "\n\nHIGH-LEVEL PIPELINE REQUEST:\n"
            + high_level_prompt
        )
    options = {
        "persist": False,
        "repair_attempts": repair_attempts,
        "include_sample_data": include_sample_data,
        "validation_mode": validation_mode,
        "generation_strategy": generation_strategy,
        "allow_deterministic_fallback": bool(payload.get("allow_deterministic_fallback")),
        "user_instruction": user_instruction,
    }
    metadata = {
        "generation_mode": str(payload.get("generation_mode") or "").strip(),
        "data_awareness": _pipeline_sample_data_summary(graph),
        "high_level_prompt": high_level_prompt,
        "options": options,
    }
    return (
        {
            "context": _build_pipeline_codegen_context(
                graph,
                include_samples=include_sample_data,
            ),
            "options": options,
            "llm_config": payload.get("llm_config"),
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
    graph_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    expected_flow_ids = [
        str(
            node.get("id")
            or _node_data(node).get("flow_id")
            or _node_data(node).get("id")
            or ""
        ).strip()
        for node in graph_nodes
        if isinstance(node, dict)
    ]
    expected_flow_ids = [flow_id for flow_id in expected_flow_ids if flow_id]
    invalid_nodes = _invalid_codegen_nodes(
        generated_nodes,
        expected_flow_ids,
    )
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
    openrouter_provider_only = _chatbot_provider_only(
        config.get("openrouterProviderOnly", config.get("openrouter_provider_only"))
    )
    codegen_openrouter_provider_only = _chatbot_provider_only(
        config.get(
            "codegenOpenrouterProviderOnly",
            config.get("codegen_openrouter_provider_only"),
        )
    )
    response = {
        "id": str(config.get("id") or ""),
        "name": str(config.get("name") or ""),
        "provider": str(config.get("provider") or "custom"),
        "model": str(config.get("model") or ""),
        "codegenModel": str(config.get("codegenModel") or config.get("codegen_model") or ""),
        "codegen_model": str(config.get("codegenModel") or config.get("codegen_model") or ""),
        "openrouterProviderOnly": openrouter_provider_only,
        "openrouter_provider_only": openrouter_provider_only,
        "codegenOpenrouterProviderOnly": codegen_openrouter_provider_only,
        "codegen_openrouter_provider_only": codegen_openrouter_provider_only,
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


def _chatbot_provider_only(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return list(dict.fromkeys(
        str(item).strip().lower()
        for item in values
        if str(item).strip()
    ))


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
    openrouter_provider_only = _chatbot_provider_only(
        payload.get(
            "openrouterProviderOnly",
            payload.get(
                "openrouter_provider_only",
                existing.get("openrouterProviderOnly", existing.get("openrouter_provider_only")),
            ),
        )
    )
    codegen_openrouter_provider_only = _chatbot_provider_only(
        payload.get(
            "codegenOpenrouterProviderOnly",
            payload.get(
                "codegen_openrouter_provider_only",
                existing.get(
                    "codegenOpenrouterProviderOnly",
                    existing.get("codegen_openrouter_provider_only"),
                ),
            ),
        )
    )
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
    if not codegen_model:
        return _json_error(400, "codegenModel is required")
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
        "openrouterProviderOnly": openrouter_provider_only,
        "openrouter_provider_only": openrouter_provider_only,
        "codegenOpenrouterProviderOnly": codegen_openrouter_provider_only,
        "codegen_openrouter_provider_only": codegen_openrouter_provider_only,
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


@app.route("/api/reusable-pipelines", methods=["GET", "POST", "DELETE", "OPTIONS"])
@require_auth
def reusable_pipelines():
    return _proxy_response(dispatch_graph_request, "neo4j_reusable_pipelines")


@app.route("/api/reusable-pipelines/version", methods=["GET", "OPTIONS"])
@require_auth
def reusable_pipeline_version():
    return _proxy_response(dispatch_graph_request, "neo4j_reusable_pipeline_version")


@app.route("/api/reusable-pipelines/attach", methods=["POST", "OPTIONS"])
@require_auth
def attach_reusable_pipeline_version():
    return _proxy_response(dispatch_graph_request, "neo4j_attach_reusable_pipeline_version")


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
            "llm_config": payload.get("llm_config"),
            "options": {
                "persist": False,
                "repair_attempts": 7,
                "include_sample_data": bool(payload.get("include_sample_data")),
                "user_instruction": str(payload.get("user_instruction") or ""),
            },
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


@app.route("/api/pipeline/external-runtime-prompt", methods=["POST", "OPTIONS"])
@require_auth
def pipeline_external_runtime_prompt():
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
        context = _build_pipeline_codegen_context(graph, include_samples=False)
        graph_nodes = context["graph"]["nodes"]
        if not graph_nodes:
            return _json_error(422, "pipeline graph has no nodes")
        high_level_prompt = str(
            payload.get("high_level_prompt")
            or payload.get("user_instruction")
            or ""
        ).strip()
        return jsonify(
            {
                "prompt": _build_external_ai_runtime_prompt(
                    graph,
                    high_level_prompt,
                ),
                "filename": "inlumen-external-ai-runtime-prompt.txt",
                "node_count": len(graph_nodes),
                "input_policy": "user_supplied",
            }
        ), 200
    except Exception as exc:
        return _json_error(502, "external AI runtime prompt preparation failed", str(exc))


@app.route("/api/pipeline/generation-runs", methods=["GET", "POST", "OPTIONS"])
@require_auth
def pipeline_generation_runs():
    if request.method == "OPTIONS":
        return _preflight_response()
    if request.method == "GET":
        try:
            limit = min(max(int(request.args.get("limit") or 20), 1), 100)
        except (TypeError, ValueError):
            limit = 20
        return jsonify(
            {
                "runs": [
                    _codegen_run_summary(record)
                    for record in CODEGEN_RUN_STORE.list(limit=limit)
                ]
            }
        ), 200
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
        now = _utc_now_iso()
        CODEGEN_RUN_STORE.put({
            "run_id": run_id,
            "status": str(codegen_run.get("status") or "queued"),
            "graph": graph,
            "metadata": metadata,
            "context_fingerprint": _codegen_context_fingerprint(
                codegen_payload["context"]
            ),
            "remote_job": _codegen_job_snapshot(codegen_run),
            "persisted": False,
            "created_at": now,
            "updated_at": now,
        })
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
        local_run = CODEGEN_RUN_STORE.get(run_id)
        if local_run is None:
            return _json_error(
                404,
                "pipeline generation run cannot be resumed; backend run metadata is unavailable",
            )
        existing_options = (
            local_run.get("metadata", {}).get("options", {})
            if isinstance(local_run.get("metadata"), dict)
            else {}
        )
        original_instruction = str(
            existing_options.get("user_instruction")
            if isinstance(existing_options, dict)
            else ""
        ).strip()
        repair_instruction = str(payload.get("user_instruction") or "").strip()
        combined_instruction = original_instruction
        if repair_instruction:
            combined_instruction += (
                "\n\nREPAIR REQUEST:\n"
                + repair_instruction
            )
        resume_payload = {
            "llm_config": payload.get("llm_config"),
            "flow_id": str(payload.get("flow_id") or "").strip() or None,
            "repair_attempts": payload.get("repair_attempts", 7),
            "user_instruction": combined_instruction,
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
                int(resume_payload["repair_attempts"] or 7),
            )
        except (TypeError, ValueError):
            options["repair_attempts"] = 7
        metadata["options"] = options
        metadata["generation_mode"] = "repair"
        now = _utc_now_iso()
        CODEGEN_RUN_STORE.put({
            "run_id": new_run_id,
            "status": str(codegen_run.get("status") or "queued"),
            "graph": local_run["graph"],
            "metadata": metadata,
            "context_fingerprint": local_run.get("context_fingerprint", ""),
            "remote_job": _codegen_job_snapshot(codegen_run),
            "persisted": False,
            "resumed_from_run_id": run_id,
            "created_at": now,
            "updated_at": now,
        })
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


@app.route(
    "/api/pipeline/generation-runs/<run_id>",
    methods=["GET", "DELETE", "OPTIONS"],
)
@require_auth
def pipeline_generation_run(run_id: str):
    if request.method == "OPTIONS":
        return _preflight_response()
    try:
        if request.method == "DELETE":
            codegen_run = _cancel_codegen_pipeline_run_request(run_id)
            local_run = CODEGEN_RUN_STORE.get(run_id)
            if local_run is not None:
                local_run["status"] = "cancelled"
                local_run["updated_at"] = _utc_now_iso()
                local_run["cancelled"] = True
                local_run["remote_job"] = _codegen_job_snapshot(codegen_run)
                CODEGEN_RUN_STORE.put(local_run)
            return jsonify(
                {
                    **codegen_run,
                    "mode": (local_run or {})
                    .get("metadata", {})
                    .get("generation_mode", ""),
                    "data_awareness": (local_run or {})
                    .get("metadata", {})
                    .get("data_awareness", {}),
                    "persistence": {"status": "cancelled"},
                }
            ), 200

        codegen_run = _get_codegen_pipeline_run_request(run_id)
        local_run = CODEGEN_RUN_STORE.get(run_id)
        if local_run is not None:
            local_run["status"] = str(codegen_run.get("status") or "running")
            local_run["updated_at"] = _utc_now_iso()
            local_run["remote_job"] = _codegen_job_snapshot(codegen_run)
            CODEGEN_RUN_STORE.put(local_run)
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
        result = (
            codegen_run.get("result")
            if isinstance(codegen_run.get("result"), dict)
            else None
        )
        if status == "cancelled":
            response_payload["persistence"] = {"status": "cancelled"}
            return jsonify(response_payload), 200
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
                ] = local_run.get("finalization_error") or (
                    "pipeline script generation failed validation"
                )
            return jsonify(response_payload), 200

        if status == "valid":
            include_samples = (
                local_run.get("metadata", {})
                .get("options", {})
                .get("include_sample_data", True)
                is not False
            )
            current_graph_response = _proxy(
                dispatch_graph_request,
                "neo4j_get_graph",
                method="GET",
                data=b"",
            )
            current_graph_response.raise_for_status()
            current_graph = _upstream_json(current_graph_response)
            current_graph = current_graph if isinstance(current_graph, dict) else {}
            current_context = _build_pipeline_codegen_context(
                current_graph,
                include_samples=include_samples,
            )
            expected_fingerprint = str(local_run.get("context_fingerprint") or "")
            if (
                expected_fingerprint
                and _codegen_context_fingerprint(current_context)
                != expected_fingerprint
            ):
                reason = (
                    "The pipeline changed while code generation was running. "
                    "Generated artifacts were not attached; start a new run for "
                    "the current graph."
                )
                local_run["persisted"] = False
                local_run["finalization_error"] = reason
                local_run["persisted_response"] = {
                    "status": "not_persisted",
                    "reason": reason,
                }
                local_run["updated_at"] = _utc_now_iso()
                CODEGEN_RUN_STORE.put(local_run)
                response_payload["persistence"] = {
                    "status": "not_persisted",
                    "reason": reason,
                    "result": local_run["persisted_response"],
                }
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
        CODEGEN_RUN_STORE.put(local_run)
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
        file_role = str(request.form.get("role") or "").strip().lower()
        if file_role not in {"code", "data"}:
            file_role = ""
        uploaded_filename = str(uploaded.filename or "").strip()
        if (
            uploaded_filename
            and file_role != "code"
            and not _is_codegen_runtime_file(uploaded_filename)
        ):
            try:
                probe, size_bytes = read_attachment_probe(uploaded.stream)
            except (OSError, ValueError) as exc:
                return _json_error(
                    422,
                    "input file could not be inspected",
                    str(exc),
                )
            input_errors = attachment_input_errors(
                uploaded_filename,
                probe,
                size_bytes=size_bytes,
            )
            if input_errors:
                return _json_error(
                    422,
                    "invalid input attachment",
                    input_errors,
                )
            uploaded.stream.seek(0)
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
                    **({"role": file_role} if file_role else {}),
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
