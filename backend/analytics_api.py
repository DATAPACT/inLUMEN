import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, has_request_context, make_response, request

from async_runtime import run_async
from auth_middleware import require_auth
from chat_state import clear_state_from_disk
from deployment_artifacts import (
    DeploymentArtifactValidationError,
    build_argo_workflow_yaml,
    build_dagster_project_files,
    build_deployment_bundle_files,
)
from deployment_agents import (
    generate_argo_yaml_from_graph,
    generate_dockerfiles_with_agent,
)
from graph_client import (
    fetch_pipeline_graph,
    fetch_pipeline_versions,
)
from llm_config import llm_config_from_payload, log_llm_selection
from node_secrets import runtime_secret_environment
from pipeline_agent.context import _clean_client_graph
from pipeline_agent.cancellation import (
    request_pipeline_turn_cancel,
    run_cancellable_pipeline_turn,
)
from pipeline_agent.service import PipelineEditorTurnCancelled, run_pipeline_editor_turn
from runtime_config import add_cors_headers

app = Flask(__name__)

CODEGEN_SERVICE_URL = os.getenv(
    "INLUMEN_CODEGEN_SERVICE_URL",
    "http://127.0.0.1:8010",
).rstrip("/")
DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS = int(
    os.getenv("INLUMEN_CODEGEN_DEPLOYMENT_TIMEOUT_SECONDS", "1800")
)


@app.after_request
def apply_cors(response):
    return add_cors_headers(response, request.headers.get("Origin"))


def _preflight_response():
    return make_response("", 200)


def _post_deployment_validation_request(
    *,
    files: list[dict],
    targets: dict,
    options: dict,
    runtime_secrets: dict[str, str] | None = None,
) -> dict:
    mode = str(options.get("mode") or "").strip().lower()
    payload = {
        "files": files,
        "targets": targets,
        "mode": mode or "validate",
        "materialize": bool(options.get("materialize", targets.get("dagster"))),
        "reinstall": bool(options.get("reinstall", False)),
        "skip_install": bool(options.get("skip_install", False)),
        "validate_argo": bool(options.get("validate_argo", targets.get("argo"))),
        "validate_dagster": bool(options.get("validate_dagster", targets.get("dagster"))),
        "argo_lint": bool(options.get("argo_lint", False)),
        "argo_dry_run": bool(options.get("argo_dry_run", False)),
        "timeout_seconds": int(
            options.get("timeout_seconds")
            or DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS
        ),
        "runtime_secrets": runtime_secrets or {},
    }
    encoded = json.dumps(payload).encode("utf-8")
    service_api_key = os.getenv("INLUMEN_CODEGEN_SERVICE_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if service_api_key:
        headers["Authorization"] = f"Bearer {service_api_key}"
    http_request = Request(
        f"{CODEGEN_SERVICE_URL}/v1/validate/deployment-bundle",
        data=encoded,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(http_request, timeout=DEPLOYMENT_VALIDATION_TIMEOUT_SECONDS) as response:
            response_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Codegen deployment validation rejected request: {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Codegen service unavailable at {CODEGEN_SERVICE_URL}: {exc}"
        ) from exc

    parsed = json.loads(response_payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Codegen deployment validation returned an invalid response")
    return parsed


def _dockerfile_inputs(files: list[dict]) -> tuple[list[str], list[str], list[str]]:
    filenames = [file["filename"] for file in files]
    buckets = [file["bucket"] for file in files]
    ids = []
    for file, bucket in zip(files, buckets):
        match = re.search(r"files-step-id-(\d+)", bucket)
        step_id = str(file.get("step_id") or "").strip()
        if match:
            ids.append(match.group(1))
        elif step_id:
            ids.append(step_id)
        else:
            raise ValueError(f"Could not extract step id from bucket '{bucket}'.")
    return filenames, buckets, ids


def _file_refs_from_version_graph(graph: dict) -> list[dict]:
    if not isinstance(graph, dict):
        return []

    refs: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
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
            filename = ""
            bucket = default_bucket
            snapshot_bucket = ""
            snapshot_object = ""
            if isinstance(item, str):
                filename = item.strip()
            elif isinstance(item, dict):
                filename = str(item.get("filename") or item.get("name") or "").strip()
                bucket = str(item.get("bucket") or default_bucket).strip().lower()
                snapshot_bucket = str(item.get("snapshot_bucket") or "").strip().lower()
                snapshot_object = str(item.get("snapshot_object") or "").strip()
            if not filename:
                continue
            key = (step_id, filename, bucket, snapshot_object)
            if key in seen:
                continue
            seen.add(key)
            ref = {
                "step_id": step_id,
                "filename": filename,
                "bucket": bucket,
            }
            if snapshot_bucket and snapshot_object:
                ref["snapshot_bucket"] = snapshot_bucket
                ref["snapshot_object"] = snapshot_object
            refs.append(ref)
    return refs


def _request_authorization_header() -> str | None:
    if not has_request_context():
        return None
    auth_header = request.headers.get("Authorization", "")
    return auth_header if auth_header else None


def _pipeline_graph_from_payload_or_backend(data: dict) -> dict:
    payload_graph = data.get("pipeline_graph")
    if isinstance(payload_graph, dict):
        return payload_graph
    return run_async(
        fetch_pipeline_graph(
            authorization=_request_authorization_header(),
        )
    )




@app.route("/agentic_generate_dockerfiles", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_dockerfiles():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}

    try:
        pipeline_graph = _pipeline_graph_from_payload_or_backend(data)
        files = _file_refs_from_version_graph(pipeline_graph)
        filenames, buckets, ids = _dockerfile_inputs(files)
        print("[analytics_api.py] Filenames received:", filenames)
        print("[analytics_api.py] Buckets received:", buckets)
        print("[analytics_api.py] Corresponding IDs to filenames that were received:", ids)

        print("[analytics_api.py] Deriving deployment files from deterministic runtime packages.")
        parsed = run_async(
            generate_dockerfiles_with_agent(
                filenames,
                ids,
                pipeline_graph=pipeline_graph,
                file_refs=files,
                require_attached_runtime=bool(data.get("require_attached_runtime")),
            )
        )
        response_payload = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed.dict()
        return jsonify(response_payload), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating dockerfiles:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/agentic_generate_yaml", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_yaml():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}
    dockerfile_json = data.get("dockerfile_json") or data.get("dockerfiles_json")
    print("[analytics_api.py] Dockerfile received:", dockerfile_json)

    try:
        print("[analytics_api.py] Generating Argo YAML with deterministic artifact builder.")
        pipeline_graph = _pipeline_graph_from_payload_or_backend(data)
        yaml_text = build_argo_workflow_yaml(pipeline_graph, dockerfile_json)
        resp = make_response(yaml_text, 200)
        resp.headers["Content-Type"] = "application/x-yaml; charset=utf-8"
        return resp
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating YAML:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/agentic_generate_dagster", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_dagster():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}
    dockerfile_json = data.get("dockerfile_json") or data.get("dockerfiles_json")

    try:
        print("[analytics_api.py] Generating Dagster project with deterministic artifact builder.")
        pipeline_graph = _pipeline_graph_from_payload_or_backend(data)
        files = build_dagster_project_files(pipeline_graph, dockerfile_json)
        return jsonify(
            {
                "files": files,
                "guardrails": {
                    "valid": True,
                    "checks": [
                        "Dagster project generated from persisted runtime artifacts",
                        "one defs.yaml component instance per executable pipeline step",
                        "graph edges mapped to Dagster asset dependencies",
                    ],
                },
            }
        ), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating Dagster project:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/agentic_generate_deployment_bundle", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_deployment_bundle():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}
    dockerfile_json = data.get("dockerfile_json") or data.get("dockerfiles_json")
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    validation_options = data.get("validation") if isinstance(data.get("validation"), dict) else {}
    validation_mode = str(
        validation_options.get("mode")
        or data.get("validation_mode")
        or data.get("deploymentValidationMode")
        or "validate"
    ).strip().lower()
    # Structural and input-contract validation is mandatory for every bundle.
    # Fast mode skips heavyweight target loading/materialization only.
    validate_bundle = True
    fast_validation = validation_mode == "fast"

    try:
        print("[analytics_api.py] Generating canonical deployment bundle.")
        pipeline_graph = _pipeline_graph_from_payload_or_backend(data)
        bundle = build_deployment_bundle_files(
            pipeline_graph,
            dockerfile_json,
            targets=targets,
        )

        validation_report = None
        repair_report = None
        run_record = None
        if validate_bundle:
            service_report = _post_deployment_validation_request(
                files=bundle["files"],
                targets=bundle["manifest"]["targets"],
                options={
                    **validation_options,
                    "mode": validation_mode,
                    **(
                        {
                            "materialize": False,
                            "validate_argo": False,
                            "validate_dagster": False,
                        }
                        if fast_validation
                        else {}
                    ),
                },
                runtime_secrets=runtime_secret_environment(pipeline_graph),
            )
            repair_report = service_report.get("repair_report")
            validation_report = service_report.get("validation_report")
            if not isinstance(validation_report, dict):
                raise RuntimeError(
                    "Codegen deployment validation omitted its validation report"
                )
            repaired_files = service_report.get("repaired_files")
            if (
                service_report.get("ok")
                and isinstance(repaired_files, list)
                and repaired_files
            ):
                bundle["files"] = repaired_files

            if not validation_report.get("ok"):
                return jsonify(
                    {
                        "error": "Deployment bundle validation failed",
                        "validation_report": validation_report,
                        "repair_report": repair_report,
                    }
                ), 422

            bundle["files"].append(
                {
                    "path": "validation/deployment-validation-report.json",
                    "filename": "deployment-validation-report.json",
                    "flow_id": "",
                    "content": json.dumps(validation_report, indent=2) + "\n",
                    "content_type": "application/json",
                    "role": "deployment-validation-report",
                }
            )
            if repair_report is not None:
                bundle["files"].append(
                    {
                        "path": "validation/deployment-repair-report.json",
                        "filename": "deployment-repair-report.json",
                        "flow_id": "",
                        "content": json.dumps(repair_report, indent=2) + "\n",
                        "content_type": "application/json",
                        "role": "deployment-repair-report",
                    }
                )

            execution_requested = (
                not fast_validation
                and bool(bundle["manifest"]["targets"].get("dagster"))
                and bool(validation_options.get("materialize", True))
            )
            if execution_requested:
                run_outputs = service_report.get("run_outputs")
                if not isinstance(run_outputs, list):
                    run_outputs = []
                bundle["files"].extend(run_outputs)
                run_record = {
                    "schema_version": "inlumen.run-result@1",
                    "run_id": uuid.uuid4().hex,
                    "status": "succeeded",
                    "engine": "dagster",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "run_spec": "run-spec.json",
                    "package_manager": (
                        ((validation_report.get("dagster") or {}).get("package_manager"))
                        if isinstance(validation_report, dict)
                        else None
                    ) or "uv",
                    "outputs": [
                        {
                            "path": output["path"],
                            "filename": output["filename"],
                            "kind": output.get("kind"),
                            "format": output.get("format"),
                            "content_type": output.get("content_type"),
                            "size_bytes": output.get("size_bytes"),
                            "sha256": output.get("sha256"),
                        }
                        for output in run_outputs
                    ],
                }
                bundle["files"].append(
                    {
                        "path": f"runs/{run_record['run_id']}/run-result.json",
                        "filename": "run-result.json",
                        "flow_id": "",
                        "content": json.dumps(run_record, indent=2) + "\n",
                        "content_type": "application/json",
                        "role": "run-result",
                    }
                )

        return jsonify(
            {
                **bundle,
                "validation_report": validation_report,
                "repair_report": repair_report,
                "run": run_record,
            }
        ), 200
    except DeploymentArtifactValidationError as exc:
        return jsonify(
            {
                "error": "Deployment bundle validation failed",
                "validation_report": {
                    "ok": False,
                    "errors": exc.errors,
                },
            }
        ), 422
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating deployment bundle:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/agentic_generate_version_yamls", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_version_yamls():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json(silent=True) or {}

    try:
        versions = run_async(fetch_pipeline_versions(
            include_graph=True,
            authorization=_request_authorization_header(),
        ))
        generated_versions = []

        for version in versions:
            graph = version.get("graph") if isinstance(version.get("graph"), dict) else {}
            file_refs = _file_refs_from_version_graph(graph)
            filenames, _buckets, ids = _dockerfile_inputs(file_refs) if file_refs else ([], [], [])
            dockerfiles = run_async(
                generate_dockerfiles_with_agent(
                    filenames,
                    ids,
                    pipeline_graph=graph,
                    file_refs=file_refs,
                )
            )
            dockerfiles_json = dockerfiles.model_dump() if hasattr(dockerfiles, "model_dump") else dockerfiles.dict()

            yaml_text = generate_argo_yaml_from_graph(
                pipeline_graph=graph,
                file_refs=file_refs,
                dockerfiles=dockerfiles_json,
            )
            generated_versions.append({
                "uid": str(version.get("uid") or ""),
                "name": str(version.get("name") or ""),
                "version": version.get("version"),
                "description": version.get("description"),
                "created_at": version.get("created_at"),
                "updated_at": version.get("updated_at"),
                "file_count": version.get("file_count"),
                "node_count": version.get("node_count"),
                "edge_count": version.get("edge_count"),
                "yaml": yaml_text,
            })

        return jsonify({"versions": generated_versions}), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating version YAMLs:", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/simple_chat", methods=["POST", "OPTIONS"])
@app.route("/agentic_pipeline_editor", methods=["POST", "OPTIONS"])
@require_auth
def agentic_pipeline_editor():
    if request.method == "OPTIONS":
        return _preflight_response()

    payload = request.get_json(force=True) or {}
    user_message = (payload.get("user_message") or "").strip()
    if not user_message:
        return jsonify({"error": "Missing user_message"}), 400
    canvas_graph = _clean_client_graph(payload.get("canvas_graph"))
    active_version_uid = str(payload.get("active_version_uid") or payload.get("version_uid") or "main").strip() or "main"
    active_version_name = str(payload.get("active_version_name") or payload.get("version_name") or "").strip()
    if active_version_uid == "main":
        active_version_name = "Main"

    session_id = payload.get("session_id") or str(uuid.uuid4())
    turn_id = str(payload.get("turn_id") or uuid.uuid4()).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", turn_id):
        return jsonify({"error": "Invalid turn_id"}), 400
    try:
        llm_config = llm_config_from_payload(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    log_llm_selection("User message sent to pipeline editor", llm_config)
    authorization = _request_authorization_header()

    try:
        turn = run_cancellable_pipeline_turn(
            turn_id,
            run_pipeline_editor_turn(
                user_message=user_message,
                canvas_graph=canvas_graph,
                active_version_uid=active_version_uid,
                active_version_name=active_version_name,
                session_id=session_id,
                llm_config=llm_config,
                authorization=authorization,
            ),
        )
        assistant_message, graph, sync = (
            turn.assistant_message,
            turn.graph,
            turn.sync,
        )
        return jsonify({
            "session_id": session_id,
            "turn_id": turn_id,
            "assistant_message": assistant_message,
            "graph": graph,
            "sync": sync,
        }), 200
    except PipelineEditorTurnCancelled as exc:
        return jsonify({
            "session_id": session_id,
            "turn_id": turn_id,
            "status": "cancelled",
            "rollback_applied": exc.rollback_applied,
            "assistant_message": (
                "Stopped. The pipeline from before this request was restored."
                if exc.rollback_applied
                else "Stopped, but the previous pipeline could not be restored automatically."
            ),
        }), 409
    except asyncio.CancelledError:
        return jsonify({
            "session_id": session_id,
            "turn_id": turn_id,
            "status": "cancelled",
            "rollback_applied": True,
            "assistant_message": "Stopped before the agent turn started.",
        }), 409
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/simple_chat/cancel", methods=["POST", "OPTIONS"])
@app.route("/agentic_pipeline_editor/cancel", methods=["POST", "OPTIONS"])
@require_auth
def agentic_pipeline_editor_cancel():
    if request.method == "OPTIONS":
        return _preflight_response()

    payload = request.get_json(force=True) or {}
    turn_id = str(payload.get("turn_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", turn_id):
        return jsonify({"error": "A valid turn_id is required"}), 400
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        clear_state_from_disk(session_id)
    result = request_pipeline_turn_cancel(turn_id)
    return jsonify({
        **result,
        "completed": False,
        "session_cleared": bool(session_id),
    }), 202


@app.route("/simple_chat/reset", methods=["POST", "OPTIONS"])
@app.route("/agentic_pipeline_editor/reset", methods=["POST", "OPTIONS"])
@require_auth
def agentic_pipeline_editor_reset():
    if request.method == "OPTIONS":
        return _preflight_response()

    payload = request.get_json(force=True) or {}
    session_id = payload.get("session_id")
    if session_id:
        clear_state_from_disk(session_id)
    return jsonify({"ok": True}), 200
