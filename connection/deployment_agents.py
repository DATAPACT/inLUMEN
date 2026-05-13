from typing import Optional

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.tools import FunctionTool
from pydantic import BaseModel, Field

from async_runtime import run_async
from deployment_artifacts import build_argo_workflow_yaml, build_dockerfile_artifacts
from graph_client import fetch_pipeline_graph
from llm_config import LLMConfig, select_model_client
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
    """Generate one validated Dockerfile per pipeline step.

    The public name is kept for API compatibility, but artifact generation is now
    deterministic and guarded instead of relying on free-form LLM output.
    """
    _ = llm_config
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

    artifact_payload = build_dockerfile_artifacts(pipeline_graph, file_refs)
    print("[deployment_agents.py] Deterministic Dockerfile artifacts generated.")
    if hasattr(ListDockerfilesResponse, "model_validate"):
        return ListDockerfilesResponse.model_validate(artifact_payload)
    return ListDockerfilesResponse.parse_obj(artifact_payload)


def _make_pipeline_design_tool(neo4j_api_base_url: str):
    def _pipeline_design_tool(params: Optional[dict] = None) -> dict:
        """FunctionTool used by the team to read the Neo4j graph export."""
        print("[deployment_agents.py] _pipeline_design_tool invoked")
        return run_async(fetch_pipeline_graph(neo4j_api_base_url))

    return _pipeline_design_tool


def _minio_codefetch_tool(params: Optional[dict] = None) -> dict:
    """Download code files referenced by the current pipeline steps."""
    payload = params or {}
    file_refs = payload.get("files", []) or []
    retrieved = []
    print("[deployment_agents.py] _minio_codefetch_tool called with entries:", file_refs)
    for entry in file_refs:
        bucket = str(entry.get("bucket") or "").lower()
        filename = str(entry.get("filename") or "")
        step_id = str(entry.get("step_id") or "")
        if not bucket or not filename:
            continue
        try:
            content = run_async(read_minio_object(bucket, filename))
        except Exception as exc:
            content = f"[ERROR: {exc}]"
        retrieved.append(
            {
                "step_id": step_id,
                "bucket": bucket,
                "filename": filename,
                "content": content,
            }
        )
        print(
            f"[deployment_agents.py] _minio_codefetch_tool read {filename} from {bucket} "
            f"(step {step_id}): {'error' if content.startswith('[ERROR') else 'success'}"
        )
    return {"files": retrieved}


def _generate_argo_yaml_tool(params: Optional[dict] = None) -> str:
    """Produce the final Argo Workflow YAML from pipeline, code, and dockerfile metadata."""
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
    print("[deployment_agents.py] _generate_argo_yaml_tool produced guarded Argo workflow.")
    return workflow_text


def build_argo_yaml_team(
    llm_config: Optional[LLMConfig],
    neo4j_api_base_url: str,
) -> RoundRobinGroupChat:
    """Construct an AutoGen team that fetches pipeline info, downloads code, and emits Argo YAML."""
    model_client = select_model_client(llm_config)
    pipeline_tool = FunctionTool(
        _make_pipeline_design_tool(neo4j_api_base_url),
        name="fetch_pipeline_design",
        description="Returns the current pipeline graph (steps, files, flows) from Neo4j.",
    )

    minio_tool = FunctionTool(
        _minio_codefetch_tool,
        name="fetch_code_file_contents",
        description="Read referenced files from MinIO and return their contents for each step.",
    )

    yaml_tool = FunctionTool(
        _generate_argo_yaml_tool,
        name="generate_argo_workflow",
        description="Convert the pipeline graph and file contents into an Argo Workflow YAML definition.",
    )

    pipeline_agent = AssistantAgent(
        name="pipeline_inspector",
        model_client=model_client,
        tools=[pipeline_tool],
        system_message="""You are the pipeline inspector. Call [fetch_pipeline_design] to grab the pipeline graph,
        including all steps, their flow IDs, labels, types, and linked buckets/files. Replay that information in
        plain language so downstream agents know which buckets/file names to fetch.""",
        max_tool_iterations=1,
        reflect_on_tool_use=True,
    )

    file_agent = AssistantAgent(
        name="code_reader",
        model_client=model_client,
        tools=[minio_tool],
        system_message="""You are the code reader. Examine the conversation to identify every step (flow_id)
        and its linked bucket/file names. Call [fetch_code_file_contents] once with all step/bucket/file pairs so we
        can capture file content, input/output hints, and dependency clues.""",
        max_tool_iterations=1,
        reflect_on_tool_use=True,
    )

    yaml_agent = AssistantAgent(
        name="argo_composer",
        model_client=model_client,
        tools=[yaml_tool],
        system_message="""You are the Argo Workflow composer. Use the pipeline graph message, the code reader
        message, and the Dockerfile metadata message to craft a single Argo Workflow YAML that sequences every step in flow order.
        Call [generate_argo_workflow] with parameters containing "pipeline_graph" set to the pipeline tool output,
        "file_contents" set to the code reader tool output, and "dockerfiles" set to the dockerfile metadata tool output.
        Return only the YAML document content, no comments.""",
        max_tool_iterations=1,
        reflect_on_tool_use=True,
    )

    return RoundRobinGroupChat(
        [pipeline_agent, file_agent, yaml_agent],
        max_turns=25,
    )
