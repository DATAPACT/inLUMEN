import asyncio
import json
import os
import re
import uuid

from flask import Flask, jsonify, make_response, request

from async_runtime import run_async
from auth_middleware import require_auth
from chat_state import clear_state_from_disk, load_state_from_disk, save_state_to_disk
from deployment_agents import generate_dockerfiles_with_agent, build_argo_yaml_team
from llm_config import llm_config_from_payload, log_llm_selection
from pipeline_editor_team import build_pipeline_editing_team
from runtime_config import default_frontend_origin, get_service_port


NEO4J_API_PORT = get_service_port("NEO4J_API_PORT", 5001)
LLM_API_PORT = get_service_port("LLM_API_PORT", 5002)
NEO4J_API_BASE_URL = (
    os.getenv("NEO4J_API_BASE_URL", "").strip()
    or f"http://127.0.0.1:{NEO4J_API_PORT}"
)
CORS_ALLOWED_ORIGIN = (
    os.getenv("CORS_ALLOWED_ORIGIN", "").strip()
    or default_frontend_origin()
)

app = Flask(__name__)


def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = CORS_ALLOWED_ORIGIN
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@app.after_request
def apply_cors(response):
    return add_cors_headers(response)


def _preflight_response():
    return make_response("", 200)


def _dockerfile_inputs(files: list[dict]) -> tuple[list[str], list[str], list[str]]:
    filenames = [file["filename"] for file in files]
    buckets = [file["bucket"] for file in files]
    ids = []
    for bucket in buckets:
        match = re.search(r"files-step-id-(\d+)", bucket)
        if not match:
            raise ValueError(f"Could not extract step id from bucket '{bucket}'.")
        ids.append(match.group(1))
    return filenames, buckets, ids


def _assistant_message_from_result(result) -> str:
    for msg in reversed(result.messages or []):
        if getattr(msg, "source", None) in ("assistant", "assistant_agent") and hasattr(msg, "content"):
            return msg.content
    if result.messages:
        return getattr(result.messages[-1], "content", "")
    return ""


@app.route("/agentic_generate_dockerfiles", methods=["POST", "OPTIONS"])
@require_auth
def agentic_generate_dockerfiles():
    if request.method == "OPTIONS":
        return _preflight_response()

    data = request.get_json() or {}
    files = data.get("files", [])

    try:
        filenames, buckets, ids = _dockerfile_inputs(files)
        print("[analytics_api.py] Filenames received:", filenames)
        print("[analytics_api.py] Buckets received:", buckets)
        print("[analytics_api.py] Corresponding IDs to filenames that were received:", ids)

        llm_config = llm_config_from_payload(data)
        log_llm_selection("Generating Dockerfiles", llm_config)
        parsed = run_async(
            generate_dockerfiles_with_agent(filenames, ids, llm_config)
        )
        return jsonify(parsed.model_dump()), 200
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
    task = data.get(
        "task",
        "Generate an Argo Workflow YAML file based on the given pipeline design.",
    )
    task_message = task
    if dockerfile_json:
        try:
            dockerfile_dump = json.dumps(dockerfile_json)
        except Exception:
            dockerfile_dump = str(dockerfile_json)
        task_message += "\n\nDockerfile metadata: " + dockerfile_dump

    async def run_team():
        llm_config = llm_config_from_payload(data)
        log_llm_selection("Generating Argo YAML", llm_config)
        team = build_argo_yaml_team(llm_config, NEO4J_API_BASE_URL)
        result = await team.run(task=task_message)
        print("[analytics_api.py] build_argo_yaml_team run result messages:")
        for idx, msg in enumerate(result.messages or []):
            source = getattr(msg, "source", None)
            content = getattr(msg, "content", None)
            print(f"  message[{idx}] source={source} content_preview={str(content)[:200]}")
        return _assistant_message_from_result(result)

    try:
        yaml_text = asyncio.run(run_team())
        resp = make_response(yaml_text, 200)
        resp.headers["Content-Type"] = "application/x-yaml; charset=utf-8"
        return resp
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print("[analytics_api.py] Error generating YAML:", exc)
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

    session_id = payload.get("session_id") or str(uuid.uuid4())
    try:
        llm_config = llm_config_from_payload(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    log_llm_selection("User message sent to pipeline editor", llm_config)

    async def run_turn():
        team = build_pipeline_editing_team(
            llm_config=llm_config,
            neo4j_api_base_url=NEO4J_API_BASE_URL,
        )
        team_state = load_state_from_disk(session_id)
        if team_state:
            await team.load_state(team_state)
        result = await team.run(task=user_message)
        new_state = await team.save_state()
        save_state_to_disk(session_id, new_state)
        return _assistant_message_from_result(result)

    try:
        assistant_message = asyncio.run(run_turn())
        return jsonify({"session_id": session_id, "assistant_message": assistant_message}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=LLM_API_PORT)
