import re
from pathlib import Path
from typing import Dict, List, Optional

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.tools import FunctionTool
from pydantic import BaseModel

from async_runtime import run_async
from graph_client import fetch_pipeline_graph
from llm_config import LLMConfig, select_model_client
from minio_gateway import read_minio_object


class ListDockerfilesResponse(BaseModel):
    class DockerfileItem(BaseModel):
        dockerfile_filename: str
        content: str

    dockerfiles: list[DockerfileItem]


async def generate_dockerfiles_with_agent(
    filenames: list[str],
    ids: list[str],
    llm_config: Optional[LLMConfig] = None,
) -> ListDockerfilesResponse:
    model_client = select_model_client(llm_config)
    dockerfile_generator = AssistantAgent(
        name="dockerfile_generator",
        model_client=model_client,
        description="An agent that generates Dockerfiles.",
        system_message=""" You will be given a list of files and another containing their corresponding IDs (files with same ID means they belong to the same folder). Generate a Dockerfile file per folder. Name them Dockerfile.<insert folder ID and no extension>. Follow the rules:
        1) Start with a base image. 2) Copy files into the container. 3) Install dependencies from requirements file (if present). 4) Make .sh files executable. 5) Set the startup command.
        See example below:
        FROM ubuntu:latest
        ENV DEBIAN_FRONTEND=noninteractive
        RUN apt-get update && apt-get install -y \
            bash \
            bc \
            && rm -rf /var/lib/apt/lists/*
        WORKDIR /app
        COPY ./<insert filename> /app/<insert filename>
        RUN chmod +x /app/<insert filename>
        CMD ["/app/<insert filename>", "10"]
        """,
        output_content_type=ListDockerfilesResponse,
    )
    result = await dockerfile_generator.run(
        task="List of files: " + str(filenames) + ". List of IDs: " + str(ids)
    )
    print("[deployment_agents.py] Dockerfile generator response:")
    print(result.messages[-1].content)
    return result.messages[-1].content


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
    """Generate Argo YAML from a provided graph instead of reading the active graph."""
    file_contents = _minio_codefetch_tool({"files": file_refs})
    dockerfile_payload = dockerfiles or {"dockerfiles": []}
    return _generate_argo_yaml_tool({
        "pipeline_graph": pipeline_graph or {},
        "file_contents": file_contents,
        "dockerfiles": dockerfile_payload,
    })


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

    def _steps_from_pipeline_graph(graph: dict) -> List[dict]:
        if graph.get("step_rows"):
            steps = []
            for row in graph.get("step_rows", []) or []:
                step = row.get("step") or {}
                flow_id = str(step.get("flow_id") or "").strip()
                if not flow_id:
                    continue
                steps.append(
                    {
                        "flow_id": flow_id,
                        "label": step.get("label", ""),
                        "description": step.get("description", ""),
                        "type": step.get("type", "custom"),
                        "files": row.get("files") or [],
                    }
                )
            return steps

        steps = []
        for node in graph.get("nodes", []) or []:
            data = node.get("data") or {}
            flow_id = str(data.get("flow_id") or node.get("id") or "").strip()
            if not flow_id:
                continue
            files = data.get("file_buckets") or []
            if not files:
                files = [
                    {"filename": filename, "bucket": f"files-step-id-{flow_id}"}
                    for filename in data.get("files", []) or []
                    if isinstance(filename, str)
                ]
            steps.append(
                {
                    "flow_id": flow_id,
                    "label": data.get("label", ""),
                    "description": data.get("description", ""),
                    "type": data.get("type", "custom"),
                    "files": files,
                }
            )
        return steps

    steps = []
    if isinstance(pipeline, dict):
        steps = _steps_from_pipeline_graph(pipeline)

    def _to_int(flow_id: str) -> int:
        try:
            return int(flow_id)
        except Exception:
            return float("inf")

    ordered_steps = sorted(steps, key=lambda s: _to_int(s["flow_id"]))

    file_lookup: Dict[str, List[dict]] = {}
    for entry in file_contents:
        step_id = str(entry.get("step_id") or "")
        file_lookup.setdefault(step_id, []).append(entry)

    dockerfile_lookup: Dict[str, List[dict]] = {}
    for entry in dockerfiles:
        name = str(
            entry.get("flow_id")
            or entry.get("step_id")
            or entry.get("dockerfile_filename")
            or entry.get("name")
            or ""
        )
        flow_match = re.search(r"(\d+)", name)
        flow_id = str(entry.get("flow_id") or entry.get("step_id") or "").strip()
        if not flow_id and flow_match:
            flow_id = flow_match.group(1)
        if flow_id:
            dockerfile_lookup.setdefault(flow_id, []).append(entry)

    def _image_name(entry: dict, flow_id: str) -> str:
        name = str(entry.get("name") or entry.get("dockerfile_filename") or "").strip()
        if not name:
            return f"inlumen/step-{flow_id}:latest"
        base = name.replace(".", "-") if name.lower().startswith("dockerfile.") else Path(name).stem
        base = re.sub(r"[^a-zA-Z0-9._-]", "-", base)
        base = base.lower()
        if not base:
            base = f"step-{flow_id}"
        return f"inlumen/{base}:latest"

    lines = [
        "apiVersion: argoproj.io/v1alpha1",
        "kind: Workflow",
        "metadata:",
        "  name: inlumen-pipeline",
        "spec:",
        "  entrypoint: pipeline-steps",
        "  templates:",
        "  - name: pipeline-steps",
        "    steps:",
    ]

    for step in ordered_steps:
        template = f"step-{step['flow_id']}"
        lines.append("      - - name: " + template)
        lines.append("           template: " + template)

    for step in ordered_steps:
        template = f"step-{step['flow_id']}"
        files = file_lookup.get(step["flow_id"], [])
        docker_entries = dockerfile_lookup.get(step["flow_id"], [])
        script_body = [
            f"# Step {step['flow_id']}: {step['label'] or 'Unnamed'}",
            f"# Type: {step['type']}",
        ]
        if docker_entries:
            script_body.append(
                f"# Dockerfiles: {', '.join([entry.get('name') or 'unknown' for entry in docker_entries])}"
            )
        if step["description"]:
            script_body.append(f"# Description: {step['description']}")
        if files:
            for idx, file_entry in enumerate(files, start=1):
                script_body.append(
                    f"# File {idx}: {file_entry['filename']} (bucket {file_entry['bucket']})"
                )
                if file_entry["content"]:
                    script_body.append("")
                    script_body.extend(file_entry["content"].splitlines() or [""])
                    script_body.append("")
        else:
            script_body.append(f'print("Executing step {step["flow_id"]} ({step["label"]})");')

        image = (
            _image_name(docker_entries[0], step["flow_id"])
            if docker_entries
            else "python:3.11"
        )

        lines.extend(
            [
                "  - name: " + template,
                "    script:",
                f"      image: {image}",
                "      command: [python]",
                "      source: |-",
            ]
        )
        for line in script_body:
            lines.append("        " + line)
        lines.append("")

    workflow_text = "\n".join(lines)
    print(
        "[deployment_agents.py] _generate_argo_yaml_tool produced Argo workflow for steps:",
        [step["flow_id"] for step in ordered_steps],
    )
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
