import inspect
import json
import re
from typing import Any, Optional

from autogen_core.models import SystemMessage, UserMessage
from pydantic import BaseModel, Field

from async_runtime import run_async
from deployment_artifacts import (
    DeploymentArtifactValidationError,
    _argo_name,
    _sanitize_fragment,
    build_argo_workflow_yaml,
    extract_pipeline_steps,
    validate_dockerfile_artifacts,
)
from llm_config import LLMConfig, resolve_llm_config, select_model_client
from minio_gateway import read_minio_object


class ListDockerfilesResponse(BaseModel):
    class DockerfileItem(BaseModel):
        dockerfile_filename: str
        content: str
        flow_id: Optional[str] = None
        image: Optional[str] = None
        command: list[str] = Field(default_factory=list)
        files: list[str] = Field(default_factory=list)

    class GuardrailReport(BaseModel):
        valid: bool
        checks: list[str] = Field(default_factory=list)

    dockerfiles: list[DockerfileItem]
    guardrails: Optional[GuardrailReport] = None


async def generate_dockerfiles_with_agent(
    filenames: list[str],
    ids: list[str],
    llm_config: Optional[LLMConfig] = None,
    pipeline_graph: Optional[dict] = None,
    file_refs: Optional[list[dict]] = None,
) -> ListDockerfilesResponse:
    """Generate one validated Dockerfile per pipeline step with an LLM."""
    resolved_config = llm_config or resolve_llm_config()
    if file_refs is None:
        if len(filenames) != len(ids):
            raise ValueError("filenames and ids must have the same length.")
        file_refs = [
            {
                "filename": filename,
                "bucket": f"files-step-id-{step_id}",
                "step_id": step_id,
            }
            for filename, step_id in zip(filenames, ids)
        ]

    steps = extract_pipeline_steps(pipeline_graph, file_refs)
    if not steps:
        raise ValueError("No pipeline steps were found for Dockerfile generation.")

    file_contents = await _fetch_dockerfile_prompt_files(file_refs)
    validation_errors: list[str] = []
    artifact_payload: dict[str, Any] | None = None

    for attempt in range(2):
        try:
            raw_payload = await _generate_dockerfiles_payload_with_llm(
                steps=steps,
                pipeline_graph=pipeline_graph or {},
                file_contents=file_contents,
                llm_config=resolved_config,
                validation_errors=validation_errors,
            )
            artifact_payload = _normalize_llm_dockerfile_payload(raw_payload, steps)
            validate_dockerfile_artifacts(
                artifact_payload["dockerfiles"],
                [step["flow_id"] for step in steps],
                steps,
            )
            break
        except DeploymentArtifactValidationError as exc:
            validation_errors = exc.errors
        except ValueError as exc:
            validation_errors = [str(exc)]
        if validation_errors:
            print(
                "[deployment_agents.py] LLM Dockerfile guardrail validation failed "
                f"on attempt {attempt + 1}: {validation_errors}"
            )
    else:
        raise DeploymentArtifactValidationError(
            "Dockerfile guardrail validation failed",
            validation_errors,
        )

    print("[deployment_agents.py] LLM Dockerfile artifacts generated and validated.")
    if hasattr(ListDockerfilesResponse, "model_validate"):
        return ListDockerfilesResponse.model_validate(artifact_payload)
    return ListDockerfilesResponse.parse_obj(artifact_payload)


async def _fetch_dockerfile_prompt_files(file_refs: Optional[list[dict]]) -> list[dict[str, str]]:
    retrieved: list[dict[str, str]] = []
    for entry in file_refs or []:
        bucket = str(entry.get("bucket") or "").lower()
        filename = str(entry.get("filename") or "")
        step_id = str(entry.get("step_id") or "")
        read_bucket = str(entry.get("snapshot_bucket") or bucket).lower()
        read_object = str(entry.get("snapshot_object") or filename)
        if not bucket or not filename:
            continue
        try:
            content = await read_minio_object(read_bucket, read_object)
        except Exception as exc:
            content = f"[ERROR: {exc}]"
        retrieved.append(
            {
                "step_id": step_id,
                "bucket": bucket,
                "filename": filename,
                "read_bucket": read_bucket,
                "read_object": read_object,
                "content": _truncate_for_prompt(content),
            }
        )
    return retrieved


def _truncate_for_prompt(content: str, max_chars: int = 12000) -> str:
    if len(content) <= max_chars:
        return content
    omitted = len(content) - max_chars
    return f"{content[:max_chars]}\n\n[TRUNCATED {omitted} CHARACTERS]"


def _dockerfile_prompt_context(
    steps: list[dict],
    pipeline_graph: dict,
    file_contents: list[dict[str, str]],
) -> dict[str, Any]:
    prompt_steps = []
    for step in steps:
        flow_id = str(step["flow_id"])
        prompt_steps.append(
            {
                "flow_id": flow_id,
                "expected_dockerfile_filename": f"Dockerfile.{_sanitize_fragment(flow_id, 'step')}",
                "label": step.get("label", ""),
                "description": step.get("description", ""),
                "type": step.get("type", ""),
                "content": step.get("content", ""),
                "endpoint": step.get("endpoint", ""),
                "database": step.get("database", ""),
                "param": step.get("param", {}),
                "files": step.get("files", []),
            }
        )
    return {
        "steps": prompt_steps,
        "edges": pipeline_graph.get("edges", []) if isinstance(pipeline_graph, dict) else [],
        "file_contents": file_contents,
    }


def _dockerfile_system_prompt() -> str:
    return """You generate production-ready Dockerfiles for inLUMEN pipeline steps.
Use natural-language understanding over each step label, description, parameters, attached filenames, and file contents.
Return only one strict JSON object. Do not return markdown, explanations, or code fences."""


def _dockerfile_user_prompt(context: dict[str, Any], validation_errors: list[str]) -> str:
    repair = ""
    if validation_errors:
        repair = (
            "\nThe previous JSON failed validation. Fix all of these issues in the new JSON:\n"
            + json.dumps(validation_errors, indent=2)
            + "\n"
        )

    return f"""Generate Dockerfile artifacts for every step in this context.

Rules:
- Return exactly this shape: {{"dockerfiles":[{{"dockerfile_filename":"Dockerfile.<step_id>","content":"...","flow_id":"<step_id>","image":"inlumen/<step-name>:latest","command":["..."],"files":["..."]}}],"guardrails":{{"valid":true,"checks":["LLM-generated Dockerfiles validated after generation"]}}}}
- Generate exactly one Dockerfile per step, using each step's expected_dockerfile_filename.
- Dockerfile content must start with FROM, include WORKDIR, copy/add attached files when present, and include CMD or ENTRYPOINT.
- Infer the runtime and install commands from attached files and contents. For example, requirements.txt means install Python requirements, package.json means install npm dependencies, shell scripts need bash/chmod, and notebooks/scripts should use a compatible runtime.
- Use JSON-array form for CMD/ENTRYPOINT where practical, and put the same array in the command field.
- Keep the output plain JSON. The Dockerfile content string must not contain markdown fences.
{repair}
Context JSON:
{json.dumps(context, indent=2)}
"""


async def _generate_dockerfiles_payload_with_llm(
    *,
    steps: list[dict],
    pipeline_graph: dict,
    file_contents: list[dict[str, str]],
    llm_config: LLMConfig,
    validation_errors: list[str],
) -> dict[str, Any]:
    model_client = select_model_client(llm_config, parallel_tool_calls=False)
    context = _dockerfile_prompt_context(steps, pipeline_graph, file_contents)
    create_kwargs: dict[str, Any] = {}
    if llm_config.supports_structured_output:
        create_kwargs["json_output"] = ListDockerfilesResponse
    elif llm_config.supports_json_output:
        create_kwargs["json_output"] = True

    try:
        result = await model_client.create(
            [
                SystemMessage(content=_dockerfile_system_prompt()),
                UserMessage(content=_dockerfile_user_prompt(context, validation_errors), source="user"),
            ],
            **create_kwargs,
        )
    finally:
        close = getattr(model_client, "close", None)
        if close:
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result

    return _coerce_llm_json_payload(result.content)


def _coerce_llm_json_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, ListDockerfilesResponse):
        if hasattr(content, "model_dump"):
            return content.model_dump()
        return content.dict()
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError(f"LLM returned unsupported Dockerfile payload type: {type(content).__name__}")

    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned invalid Dockerfile JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LLM Dockerfile JSON must be an object.")
    return parsed


def _normalize_llm_dockerfile_payload(payload: dict[str, Any], steps: list[dict]) -> dict[str, Any]:
    dockerfiles = payload.get("dockerfiles")
    if not isinstance(dockerfiles, list):
        raise ValueError("LLM Dockerfile JSON must contain a dockerfiles array.")

    step_by_id = {str(step["flow_id"]): step for step in steps}
    normalized = []
    for item in dockerfiles:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        elif hasattr(item, "dict"):
            item = item.dict()
        if not isinstance(item, dict):
            normalized.append(item)
            continue

        flow_id = str(item.get("flow_id") or item.get("step_id") or "").strip()
        if not flow_id:
            match = re.match(
                r"^Dockerfile\.([A-Za-z0-9][A-Za-z0-9_.-]*)$",
                str(item.get("dockerfile_filename") or ""),
            )
            flow_id = match.group(1) if match else ""
        step = step_by_id.get(flow_id, {})
        files = item.get("files")
        if not isinstance(files, list):
            files = [entry["filename"] for entry in step.get("files") or []]
        normalized.append(
            {
                **item,
                "flow_id": flow_id,
                "dockerfile_filename": str(item.get("dockerfile_filename") or ""),
                "content": str(item.get("content") or ""),
                "image": str(item.get("image") or f"inlumen/{_argo_name(flow_id)}:latest"),
                "command": item.get("command") if isinstance(item.get("command"), list) else [],
                "files": [str(name) for name in files],
            }
        )

    return {
        "dockerfiles": normalized,
        "guardrails": {
            "valid": True,
            "checks": [
                "LLM generated Dockerfile content from pipeline context and attached files",
                "one Dockerfile per pipeline step",
                "Dockerfiles passed deterministic format guardrails after generation",
            ],
        },
    }


def _minio_codefetch_tool(params: Optional[dict] = None) -> dict:
    """Download code files referenced by the current pipeline steps for deterministic YAML metadata."""
    payload = params or {}
    file_refs = payload.get("files", []) or []
    retrieved = []
    print("[deployment_agents.py] _minio_codefetch_tool called with entries:", file_refs)
    for entry in file_refs:
        bucket = str(entry.get("bucket") or "").lower()
        filename = str(entry.get("filename") or "")
        step_id = str(entry.get("step_id") or "")
        read_bucket = str(entry.get("snapshot_bucket") or bucket).lower()
        read_object = str(entry.get("snapshot_object") or filename)
        if not bucket or not filename:
            continue
        try:
            content = run_async(read_minio_object(read_bucket, read_object))
        except Exception as exc:
            content = f"[ERROR: {exc}]"
        retrieved.append(
            {
                "step_id": step_id,
                "bucket": bucket,
                "filename": filename,
                "read_bucket": read_bucket,
                "read_object": read_object,
                "content": content,
            }
        )
        print(
            f"[deployment_agents.py] _minio_codefetch_tool read {read_object} from {read_bucket} "
            f"(step {step_id}): {'error' if content.startswith('[ERROR') else 'success'}"
        )
    return {"files": retrieved}


def generate_argo_yaml_from_graph(
    pipeline_graph: dict,
    file_refs: list[dict],
    dockerfiles: dict | list[dict] | None = None,
) -> str:
    """Generate Argo YAML deterministically from graph and Dockerfile metadata."""
    file_contents = _minio_codefetch_tool({"files": file_refs})
    dockerfile_payload = dockerfiles or {"dockerfiles": []}
    return _generate_argo_yaml_tool({
        "pipeline_graph": pipeline_graph or {},
        "file_contents": file_contents,
        "dockerfiles": dockerfile_payload,
    })


def _generate_argo_yaml_tool(params: Optional[dict] = None) -> str:
    """Produce the final Argo Workflow YAML from pipeline, code, and Dockerfile metadata."""
    payload = params or {}
    pipeline = payload.get("pipeline_graph") or {}
    file_contents_raw = payload.get("file_contents") or []
    dockerfiles_raw = payload.get("dockerfiles") or []
    file_contents = (
        file_contents_raw.get("files", [])
        if isinstance(file_contents_raw, dict)
        else file_contents_raw
    )
    dockerfiles = (
        dockerfiles_raw.get("dockerfiles", [])
        if isinstance(dockerfiles_raw, dict)
        else dockerfiles_raw
    )
    workflow_text = build_argo_workflow_yaml(pipeline, {"dockerfiles": dockerfiles}, file_contents)
    print("[deployment_agents.py] _generate_argo_yaml_tool produced deterministic Argo workflow.")
    return workflow_text
