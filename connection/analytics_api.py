from flask import Flask, request, jsonify, Response, make_response
from auth_middleware import require_auth
from autogen_ext.code_executors.local import LocalCommandLineCodeExecutor
from autogen_ext.code_executors.docker import DockerCommandLineCodeExecutor
from autogen_agentchat.tools import AgentTool, TeamTool
from autogen_agentchat.conditions import TextMessageTermination, SourceMatchTermination, MaxMessageTermination, TextMentionTermination, FunctionalTermination
from autogen_agentchat.agents import AssistantAgent, CodeExecutorAgent, UserProxyAgent
from autogen_core.model_context import BufferedChatCompletionContext, ChatCompletionContext, TokenLimitedChatCompletionContext
from autogen_agentchat.ui import Console
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage, ModelClientStreamingChunkEvent, HandoffMessage
from autogen_core.tools import BaseTool, FunctionTool, Workbench,  StaticStreamWorkbench, ToolResult
from autogen_ext.tools.code_execution import PythonCodeExecutionTool
from autogen_core.models import CreateResult, SystemMessage, ChatCompletionClient, UserMessage, FunctionExecutionResultMessage, FunctionExecutionResult
from autogen_core import CancellationToken,  Component, ComponentModel, FunctionCall
from autogen_agentchat.base import Response as AgentResponse
from autogen_agentchat.base import Handoff as HandoffBase
from autogen_agentchat.tools import AgentTool, TeamTool
# from autogen_ext.code_executors.jupyter import JupyterCodeExecutor
from autogen_agentchat.teams import RoundRobinGroupChat, SelectorGroupChat, Swarm
from autogen_core.memory import ListMemory, MemoryContent, MemoryMimeType, Memory
from pathlib import Path
import tempfile
import io
import sys
import os
import urllib.request
import magic
import shutil
import requests
import json
import csv
import time
import re
import uuid, json
from pydantic import BaseModel, Field
from typing import Annotated, Literal, List, Optional, Union, Sequence, Any, Callable, Dict, AsyncGenerator
from datetime import datetime, timedelta, timezone
from getpass import getpass
import asyncio
import nest_asyncio
from neo4j import GraphDatabase
from minio import Minio
from minio.error import S3Error
from runtime_config import default_frontend_origin, get_minio_settings, get_service_port
from llm_config import (
    LLMConfig,
    llm_config_from_payload as _llm_config_from_payload,
    log_llm_selection as _log_llm_selection,
    select_model_client as _select_model_client,
)

# _________________Create a single global event loop for all requests____________
global_loop = asyncio.new_event_loop()
nest_asyncio.apply()
asyncio.set_event_loop(global_loop)

def run_async(coro):
    """Run async coroutine safely using the persistent global loop."""
    global global_loop
    if global_loop.is_closed():
        # Recreate if something closed it accidentally
        global_loop = asyncio.new_event_loop()
        nest_asyncio.apply()
        asyncio.set_event_loop(global_loop)
    return global_loop.run_until_complete(coro)

#_________________Service Configuration_________________
NEO4J_API_PORT = get_service_port("NEO4J_API_PORT", 5001)
LLM_API_PORT = get_service_port("LLM_API_PORT", 5002)
NEO4J_API_BASE_URL = os.getenv("NEO4J_API_BASE_URL", "").strip() or f"http://127.0.0.1:{NEO4J_API_PORT}"
CORS_ALLOWED_ORIGIN = os.getenv("CORS_ALLOWED_ORIGIN", "").strip() or default_frontend_origin()

#_________________MinIO Access_________________
MINIO_CLIENT: Optional[Minio] = None
def _get_minio_client() -> Minio:
    """Lazily load a MinIO client using local config."""
    global MINIO_CLIENT
    if MINIO_CLIENT is not None:
        return MINIO_CLIENT
    endpoint, access_key, secret_key, secure = get_minio_settings()
    MINIO_CLIENT = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )
    return MINIO_CLIENT

#_________________File state_________________
STATE_DIR = Path("./state")
STATE_DIR.mkdir(exist_ok=True)
def _state_file(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.json"
def load_state_from_disk(session_id: str):
    p = _state_file(session_id)
    if not p.exists():
        return None
    return json.loads(p.read_text("utf-8"))
def save_state_to_disk(session_id: str, team_state):
    _state_file(session_id).write_text(json.dumps(team_state), encoding="utf-8")

# TODO: Move below within build function
async def _fetch_pipeline_graph() -> dict:
    """Fetch the current pipeline nodes, files and flows from Neo4j."""
    api_url = f"{NEO4J_API_BASE_URL}/neo4j_get_graph"
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: requests.get(api_url, timeout=60)
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load pipeline graph from Neo4j ({api_url}): {exc}"
        ) from exc

async def _read_minio_object(bucket_name: str, object_name: str) -> str:
    """Return the text content of a MinIO object."""
    def _sync_read() -> str:
        client = _get_minio_client()
        response = client.get_object(bucket_name, object_name)
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()
        return data.decode("utf-8", errors="ignore")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_read)

class ForcedAssistantAgent(AssistantAgent):
    """AssistantAgent that always enforces tool calling."""
    @classmethod
    async def _call_llm(
        cls,
        model_client: ChatCompletionClient,
        model_client_stream: bool,
        system_messages: List[SystemMessage],
        model_context: ChatCompletionContext,
        workbench: Sequence[Workbench],
        handoff_tools: List[BaseTool[Any, Any]],
        agent_name: str,
        cancellation_token: CancellationToken,
        output_content_type: type[BaseModel] | None,
        message_id: str,
    ) -> AsyncGenerator[Union[CreateResult, ModelClientStreamingChunkEvent], None]:
        """Call the language model with given context and configuration.
        Args:
            model_client: Client for model inference
            model_client_stream: Whether to stream responses
            system_messages: System messages to include
            model_context: Context containing message history
            workbench: Available workbenches
            handoff_tools: Tools for handling handoffs
            agent_name: Name of the agent
            cancellation_token: Token for cancelling operation
            output_content_type: Optional type for structured output
        Returns:
            Generator yielding model results or streaming chunks
        """
        all_messages = await model_context.get_messages()
        llm_messages = cls._get_compatible_context(model_client=model_client, messages=system_messages + all_messages)
        tools = [tool for wb in workbench for tool in await wb.list_tools()] + handoff_tools
        if model_client_stream:
            model_result: Optional[CreateResult] = None
            async for chunk in model_client.create_stream(
                llm_messages,
                tools=tools,
                tool_choice="required",   # Needs to be added to enforce tool call!
                json_output=output_content_type,
                cancellation_token=cancellation_token,
            ):
                if isinstance(chunk, CreateResult):
                    model_result = chunk
                elif isinstance(chunk, str):
                    yield ModelClientStreamingChunkEvent(content=chunk, source=agent_name, full_message_id=message_id)
                else:
                    raise RuntimeError(f"Invalid chunk type: {type(chunk)}")
            if model_result is None:
                raise RuntimeError("No final model result in streaming mode.")
            yield model_result
        else:
            model_result = await model_client.create(
                llm_messages,
                tools=tools,
                tool_choice="required", # Needs to be added to enforce tool call!
                cancellation_token=cancellation_token,
                json_output=output_content_type,
            )
            yield model_result

def build_pipeline_editing_team(llm_config: LLMConfig) -> RoundRobinGroupChat:
    _log_llm_selection("Building pipeline editing team", llm_config)
    # Configure the model client used behind the agents according to selection:
    model_client = _select_model_client(llm_config)
    # Database Schema (METAMODEL) - TODO: hidden for now
    DB_SCHEMA = """
        Nodes:
        (:PIPELINE) represents one AI/data workflow. Properties:
            - uid: string (generated via randomUUID)
            - label: string
            - description: string
            - version: string
            - created_at: datetime
            - updated_at: datetime
            - status: string ("design"|"simulated"|"runtime") - default "design"
        (:STEP) represents a single node in the pipeline graph. Properties:
            - uid: string (generated via randomUUID)
            - flow_id: string (unique int: number of step generated: 1,2 ... N)
            - type: string ("input"|"config"|"output"|"action"|"storage"|"api")
            - label: string 
            - description: string
            - content: string 
            - has_files: string ("yes"|"no") - default: "no"
            - endpoint: string
            - database: string - default : "minio"
            - param_json: string - default "{}"
        (:FILE) represents a single file associated with a step. Properties:
            - uid: string (generated via randomUUID)
            - filename: string
            - added_at: datetime
            - bucket: string
        Relationships:
        (:PIPELINE)-[:HAS_STEP]->(:STEP)
        (:STEP)-[:FLOWS_TO]->(:STEP)
        (:STEP)-[:HAS_FILE]->(:FILE)
    """
    # TODO: Add graph observer
    # observer_memory = ListMemory()
    # current_time_str = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
    # observer_memory.add(MemoryContent(content=f"Graph content at time (UTC) {current_time_str} is:" + "\{\}", mime_type=MemoryMimeType.TEXT))
    # Function to allow execution of Neo4J queries:
    async def run_query(query: str,  query_type:str) -> str:
        """Run a Cypher query against Neo4j and return results. Returns String representation."""
        try:
            print("[analytics_api.py] Executing Neo4J query of type: "+ query_type)
            api_url = f"{NEO4J_API_BASE_URL}/neo4j_run_query"
            payload = {"query": query}
            headers = {"Content-Type": "application/json"}
            # Run the API call in a thread (since requests is blocking)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(api_url, data=json.dumps(payload), headers=headers)
            )
            # Check response and parse content:
            if response.status_code == 200:
                records = response.text 
                return repr(records)
            else:
                return repr({"Error": f"{response.status_code} - {response.text}"})
        except Exception as e:
            return repr({"error": str(e)})
    # Unused but helpful function:
    async def list_pipelines() -> str:
        """ Lists all pipelines and the number of steps they have."""
        try:
            query_type = "list_pipelines"
            query = f"""
            MATCH (p:PIPELINE)
            OPTIONAL MATCH (p)-[:HAS_STEP]->(s:STEP)
            RETURN p, count(DISTINCT s) AS step_count
            ORDER BY p.name;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
     # Unused but helpful function:
    async def get_pipeline_steps(pipeline_uid: str) -> str:
        """Gets the steps present in a pipeline."""
        try:
            query_type = "get_pipeline_steps"
            query = f"""
            MATCH (p:PIPELINE {{uid: '{pipeline_uid}'}})-[:HAS_STEP]->(s:STEP)
            OPTIONAL MATCH (s)-[r:FLOWS_TO]->(t:STEP)
            RETURN p, s, r, t;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
     # Unused but helpful function:
    async def inspect_step(step_uid: str) -> str:
        """ Inspects a step: returns incoming/outgoing neighbors and used files."""
        try:
            query_type = "inspect_step"
            query = f"""
            MATCH (s:STEP {{uid: '{step_uid}'}})
            OPTIONAL MATCH (prev:STEP)-[rin:FLOWS_TO]->(s)
            OPTIONAL MATCH (s)-[rout:FLOWS_TO]->(next:STEP)
            OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
            RETURN s,
            collect(DISTINCT {{prev: prev, rel: rin}})  AS incoming_neighbors,
            collect(DISTINCT {{next: next, rel: rout}}) AS outgoing_neighbors,
            collect(DISTINCT f)                         AS used_files;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
    # Dedicated function to obtain overview:
    async def overview() -> str:
        """Gives an overview of the pipeline, the present steps and files linked within."""
        try:
            query_type = "overview"
            query = """
            MATCH (p:PIPELINE)
            OPTIONAL MATCH (p)-[hs:HAS_STEP]->(s:STEP)
            OPTIONAL MATCH (s)-[r:FLOWS_TO]->(t:STEP)
            OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
            RETURN
            p { .*,
                created_at: toString(p.created_at),
                updated_at: toString(p.updated_at)
                } AS pipeline,
            s AS step,
            hs AS step_link,
            CASE
                WHEN s IS NULL OR s.flow_id IS NULL THEN NULL
                WHEN toString(s.flow_id) =~ '^[0-9]+$' THEN toInteger(s.flow_id)
                ELSE NULL
            END AS step_order,
            r AS flow,
            t AS next_step,
            collect(
                DISTINCT f { .*,
                added_at: toString(f.added_at)
                }
            ) AS files_linked_to_step
            ORDER BY pipeline.label, step_order;
            """
            result = await run_query(query, query_type)
            # TODO Update memory of observer: 
            # current_time_str = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
            # await observer_memory.add(MemoryContent(content=f"Graph content at time (UTC) {current_time_str} is:" + result, mime_type=MemoryMimeType.TEXT))
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
    # Dedicated function to create a pipeline:
    async def create_pipeline(params: str) -> str:
        """ Creates a PIPELINE node.
        params JSON:
        {
        "name": "<string>",
        "description": "<string>",
        "version": "<x.x>"
        }
        """
        try:
            query_type = "create_pipeline"
            data = json.loads(params)
            name        = data.get("name", "").replace("'", "\\'")
            description = data.get("description", "").replace("'", "\\'")
            version     = str(data.get("version", "1.0")).replace("'", "\\'")
            query = f"""
            CREATE (p:PIPELINE {{
            uid:        randomUUID(),
            name:       '{name}',
            description:'{description}',
            version:    '{version}',
            created_at: datetime(),
            updated_at: datetime(),
            status:     'design'
            }})
            RETURN {{
            uid: p.uid,
            name: p.name,
            description: p.description,
            version: p.version,
            status: p.status,
            created_at: toString(p.created_at),
            updated_at: toString(p.updated_at)
            }} AS pipeline;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
    # Dedicated function to create a step:
    #TODO Extend to any position 
    async def create_step(params: str) -> str:
        """Creates new STEP and connects it after the last STEP (if any is present).
        params JSON:
        {
        "label": "<string>",
        "description": "<string>",
        "type": "action|input|output|config|storage|api|custom"
        }
        """
        try:
            query_type = "create_step"
            data = json.loads(params)
            step_type = str(data.get("type", "")).replace("'", "\\'").strip()
            step_type_lower = step_type.lower()
            label = str(data.get("label", "")).replace("'", "\\'")
            description = str(data.get("description", "")).replace("'", "\\'")
            props_lines = [
                "uid:        randomUUID()",
                f"type:       '{step_type}'",
                f"label:      '{label}'",
                f"description:'{description}'"
            ]
            if step_type_lower == "input":
                props_lines.append("content: ''")
                props_lines.append("has_files: 'no'")
            elif step_type_lower == "config":
                props_lines.append("param_json: {}")
            elif step_type_lower == "action":
                props_lines.append("has_files: 'no'")
            elif step_type_lower == "storage":
                props_lines.append("endpoint: ''")
                props_lines.append("database: 'minio'")
            elif step_type_lower == "api":
                props_lines.append("endpoint: ''")
            elif step_type_lower == "output":
                props_lines.append("content: ''")
                props_lines.append("has_files: 'no'")
            elif step_type_lower == "custom":
                props_lines.append("has_files: 'no'")
            props_str = ",\n            ".join(props_lines)
            query = f"""
            MATCH (p:PIPELINE {{status:'design'}})
            SET p.updated_at = datetime() 
            WITH p
            OPTIONAL MATCH (sAll:STEP)
            WHERE sAll.flow_id IS NOT NULL AND toString(sAll.flow_id) =~ '^[0-9]+$'
            WITH p, coalesce(max(toInteger(sAll.flow_id)), 0) + 1 AS nextFlowId

            OPTIONAL MATCH (prev:STEP)
            WHERE prev.flow_id IS NOT NULL AND toString(prev.flow_id) =~ '^[0-9]+$'
            WITH p, nextFlowId, prev
            ORDER BY toInteger(prev.flow_id) DESC
            WITH p, nextFlowId, head(collect(prev)) AS prev

            WITH p, nextFlowId, prev,
                coalesce(prev.x, 0.0) AS prevX,
                coalesce(prev.y, 0.0) AS prevY
            CREATE (s:STEP {{
            uid: randomUUID(),
            {props_str},
            flow_id: toString(nextFlowId),
            x: CASE WHEN prev IS NULL THEN 0.0 ELSE prevX + 300.0 END,
            y: CASE WHEN prev IS NULL THEN 0.0 ELSE prevY END
            }})
            MERGE (p)-[:HAS_STEP]->(s)
            FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
            MERGE (prev)-[:FLOWS_TO]->(s)
            )
            RETURN {{
            flow_id: s.flow_id,
            uid: s.uid,
            type: s.type,
            label: s.label,
            description: s.description,
            x: s.x,
            y: s.y,
            pipeline_updated_at: toString(p.updated_at)
            }} AS step;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
    # Dedicated function to remove a step:
    async def delete_step(params: str) -> str:
        """Deletes a STEP.
        params JSON:
        {
        "step_uid": "<uid>"
        }
        """
        try:
            query_type = "delete_step"
            data = json.loads(params)
            step_uid = data["step_uid"].replace("'", "\\'")

            query = f"""
            MATCH (s:STEP {{uid: '{step_uid}'}})
            OPTIONAL MATCH (prev:STEP)-[rin:FLOWS_TO]->(s)
            OPTIONAL MATCH (s)-[rout:FLOWS_TO]->(next:STEP)
            WITH s,
                collect(DISTINCT prev) AS prevs,
                collect(DISTINCT next) AS nexts,
                collect(rin)           AS r_in,
                collect(rout)          AS r_out
            FOREACH (p IN prevs |
            FOREACH (n IN nexts |
                MERGE (p)-[:FLOWS_TO]->(n)
                )
            )
            WITH s, r_in, r_out
            FOREACH (r IN r_in | DELETE r)
            FOREACH (r IN r_out | DELETE r)
            WITH s
            OPTIONAL MATCH (s)<-[hs:HAS_STEP]-(p:PIPELINE)
            WITH s, collect(DISTINCT p) AS pipelines, collect(DISTINCT hs) AS hs_rels
            FOREACH (rel IN hs_rels | DELETE rel)
            FOREACH (pl IN pipelines | SET pl.updated_at = datetime())
            DETACH DELETE s;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
    # TODO Add change property of STEP node function.
    # Team building:
    user_proxy = UserProxyAgent("user_proxy")
    pipeline_editor = AssistantAgent(
        name = "pipeline_editor",
        model_client = model_client,
        tools = [create_pipeline, create_step, delete_step, overview], 
        description = f"An agent that designs AI/data pipelines given a user request.",
        system_message = f""" You design AI/data pipelines using your registered tools. Call one or multiple tools to create or modify a pipeline as requested by the user.
                          A PIPELINE is composed of one or several STEPs. Use overview to check if there are any pipelines. If the user request is unclear or incomplete, ask for more details.
                        - [overview]: calling this tool will give you an overview of the current pipeline content, if any. 
                        - [create_pipeline]: calling this tool will create a pipeline. 
                        - [create_step]: calling this tool will create a new step in a pipeline (will always place it last).
                        - [delete_step]: calling this tool will delete a step in a pipeline.
                        Tool calls MUST use a single string argument named params. The value of params MUST be a JSON-encoded string matching the "params JSON" schema in the docstring.
                        """,  
        max_tool_iterations = 10,
        reflect_on_tool_use = True,
    )
    # TODO: Add observer
    #pipeline_observer = AssistantAgent(
    #    name = "pipeline_observer",
    #    model_client = model_client,
    #    tools = [overview], 
    #    description = f"An agent that reports changes or modifications recorded in a graph database.",
    #    system_message = f"""Each time you are called, you call the [overview] tool to get an overview of the current graph content. Return "NO" if there are no changes from the previous version. Return "YES" if you see any differences from the previous version. """,  
    #    max_tool_iterations = 1,
    #    reflect_on_tool_use = True,
    #    model_context = BufferedChatCompletionContext(buffer_size=3),  # Does not need any context (save resources)
    #    memory=[observer_memory],
    # )
    return RoundRobinGroupChat([pipeline_editor], max_turns=1)

def _pipeline_design_tool(params: Optional[dict] = None) -> dict:
    """FunctionTool used by the team to read the Neo4j graph export."""
    print("[analytics_api.py] _pipeline_design_tool invoked")
    return run_async(_fetch_pipeline_graph())

def _minio_codefetch_tool(params: Optional[dict] = None) -> dict:
    """Download code files referenced by the current pipeline steps."""
    payload = params or {}
    file_refs = payload.get("files", []) or []
    retrieved = []
    print("[analytics_api.py] _minio_codefetch_tool called with entries:", file_refs)
    for entry in file_refs:
        bucket = str(entry.get("bucket") or "").lower()
        filename = str(entry.get("filename") or "")
        step_id = str(entry.get("step_id") or "")
        if not bucket or not filename:
            continue
        try:
            content = run_async(_read_minio_object(bucket, filename))
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
            f"[analytics_api.py] _minio_codefetch_tool read {filename} from {bucket} "
            f"(step {step_id}): {'error' if content.startswith('[ERROR') else 'success'}"
        )
    return {"files": retrieved}


def _generate_argo_yaml_tool(params: Optional[dict] = None) -> str:
    """Produce the final Argo Workflow YAML from pipeline, code, and dockerfile metadata."""
    payload = params or {}
    pipeline = payload.get("pipeline_graph") or {}
    file_contents = payload.get("file_contents") or []
    dockerfiles = payload.get("dockerfiles") or []

    steps = []
    for row in pipeline.get("step_rows", []) or []:
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
        flow_id = str(entry.get("flow_id") or "").strip()
        if flow_id:
            dockerfile_lookup.setdefault(flow_id, []).append(entry)

    def _image_name(entry: dict, flow_id: str) -> str:
        name = str(entry.get("name") or "").strip()
        if not name:
            return f"inlumen/step-{flow_id}:latest"
        base = Path(name).stem if "." in name else name
        base = re.sub(r"[^a-zA-Z0-9._-]", "-", base)
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
        script_body = []
        script_body.append(f"# Step {step['flow_id']}: {step['label'] or 'Unnamed'}")
        script_body.append(f"# Type: {step['type']}")
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
            else f"python:3.11"
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
        "[analytics_api.py] _generate_argo_yaml_tool produced Argo workflow for steps:",
        [step["flow_id"] for step in ordered_steps],
    )
    return workflow_text


def build_argo_yaml_team(llm_config: Optional[LLMConfig] = None) -> RoundRobinGroupChat:
    """Construct an AutoGen team that fetches pipeline info, downloads code, and emits Argo YAML."""
    model_client = _select_model_client(llm_config)
    pipeline_tool = FunctionTool(
        _pipeline_design_tool,
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
        max_turns=25,  # cap overall iterations to protect against loops
    )

app = Flask(__name__)

# Define a function to set the CORS headers
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = CORS_ALLOWED_ORIGIN
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'  # Adjust as needed
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# Apply the CORS function to all routes using the after_request decorator
@app.after_request
def apply_cors(response):
    return add_cors_headers(response)

class ListDockerfilesResponse(BaseModel):
    class DockerfileItem(BaseModel):
        dockerfile_filename: str
        content: str
    dockerfiles: list[DockerfileItem]

async def _generate_dockerfiles_with_agent(
    filenames: list[str],
    ids: list[str],
    llm_config: Optional[LLMConfig] = None,
) -> ListDockerfilesResponse:
    model_client = _select_model_client(llm_config)
    dockerfile_generator = AssistantAgent(
        name="dockerfile_generator",
        model_client=model_client,
        description="An agent that generates Dockerfiles.",
        system_message=f""" You will be given a list of files and another containing their corresponding IDs (files with same ID means they belong to the same folder). Generate a Dockerfile file per folder. Name them Dockerfile.<insert folder ID and no extension>. Follow the rules:
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
    result = await dockerfile_generator.run(task="List of files: " + str(filenames) + ". List of IDs: " +  str(ids))
    print("[analytics_api.py] Dockerfile generator response:")
    print(result.messages[-1].content)
    return result.messages[-1].content

@app.route('/agentic_generate_dockerfiles', methods=['POST', 'OPTIONS'])
@require_auth
def agentic_generate_dockerfiles():
    # Preflight
    if request.method == 'OPTIONS':
        return make_response("", 200)
    data = request.get_json() or {}
    files = data.get("files", [])
    filenames = [f["filename"] for f in files]
    buckets = [f["bucket"] for f in files]
    ids = [re.search(r'files-step-id-(\d+)', item).group(1) for item in buckets]
    print("[analytics_api.py] Filenames received:", filenames)
    print("[analytics_api.py] Buckets received:", buckets)
    print("[analytics_api.py] Corresponding IDs to filenames that were received:", ids)
    try:
        llm_config = _llm_config_from_payload(data)
        _log_llm_selection("Generating Dockerfiles", llm_config)
        parsed: ListDockerfilesResponse = run_async(
            _generate_dockerfiles_with_agent(filenames, ids, llm_config)
        )
        return jsonify(parsed.model_dump()), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print("[analytics_api.py] Error generating dockerfiles:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/agentic_generate_yaml', methods=['POST', 'OPTIONS'])
@require_auth
def agentic_generate_yaml(): 
    # Preflight
    if request.method == 'OPTIONS':
        return make_response("", 200)
    data = request.get_json() or {}
    dockerfile_json = data.get("dockerfile_json") or data.get("dockerfiles_json")
    print("[analytics_api.py] Dockerfile received:", dockerfile_json)
    task = data.get(
        "task",
        "Generate an Argo Workflow YAML file based on the given pipeline design.",
    )
    task_message = task
    # Add dockerfiles to task, if they exist
    if dockerfile_json:
        try:
            dockerfile_dump = json.dumps(dockerfile_json)
        except Exception:
            dockerfile_dump = str(dockerfile_json)
        task_message += "\n\nDockerfile metadata: " + dockerfile_dump

    async def run_team():
        llm_config = _llm_config_from_payload(data)
        _log_llm_selection("Generating Argo YAML", llm_config)
        team = build_argo_yaml_team(llm_config)
        result = await team.run(task=task_message)
        print("[analytics_api.py] build_argo_yaml_team run result messages:")
        for idx, msg in enumerate(result.messages or []):
            source = getattr(msg, "source", None)
            content = getattr(msg, "content", None)
            print(f"  message[{idx}] source={source} content_preview={str(content)[:200]}")
        final_message = ""
        for msg in reversed(result.messages or []):
            if getattr(msg, "source", None) in ("assistant", "assistant_agent") and hasattr(msg, "content"):
                final_message = msg.content
                break
        if not final_message and result.messages:
            final_message = getattr(result.messages[-1], "content", "")
        return final_message
    try:
        yaml_text = asyncio.run(run_team())
        resp = make_response(yaml_text, 200)
        resp.headers["Content-Type"] = "application/x-yaml; charset=utf-8"
        return resp
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print("[analytics_api.py] Error generating YAML:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/simple_chat", methods=["POST", "OPTIONS"])
@app.route("/agentic_pipeline_editor", methods=["POST", "OPTIONS"])
@require_auth
def agentic_pipeline_editor():
    if request.method == "OPTIONS":
        return make_response("", 200)  # preflight OK
    payload = request.get_json(force=True) or {}
    user_message = (payload.get("user_message") or "").strip()
    if not user_message:
        return jsonify({"error": "Missing user_message"}), 400
    session_id = payload.get("session_id") or str(uuid.uuid4())
    try:
        llm_config = _llm_config_from_payload(payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _log_llm_selection("User message sent to pipeline editor", llm_config)
    async def run_turn():
        team = build_pipeline_editing_team(llm_config=llm_config)
        team_state = load_state_from_disk(session_id)
        if team_state:
            await team.load_state(team_state)
        result = await team.run(task=user_message)
        new_state = await team.save_state()
        save_state_to_disk(session_id, new_state)
        assistant_text = ""
        for msg in reversed(result.messages or []):
            if getattr(msg, "source", None) in ("assistant", "assistant_agent") and hasattr(msg, "content"):
                assistant_text = msg.content
                break
        if not assistant_text and result.messages:
            assistant_text = getattr(result.messages[-1], "content", "")
        return assistant_text
    try:
        assistant_message = asyncio.run(run_turn())
        return jsonify({"session_id": session_id, "assistant_message": assistant_message}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/simple_chat/reset", methods=["POST", "OPTIONS"])
@app.route("/agentic_pipeline_editor/reset", methods=["POST", "OPTIONS"])
@require_auth
def agentic_pipeline_editor_reset():
    if request.method == "OPTIONS":
        return make_response("", 200)  # preflight OK
    payload = request.get_json(force=True) or {}
    session_id = payload.get("session_id")
    if session_id:
        p = _state_file(session_id)
        if p.exists():
            p.unlink()
    return jsonify({"ok": True}), 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=LLM_API_PORT)
