from flask import Flask, request, jsonify, Response, make_response
from openai import OpenAI
from autogen_ext.code_executors.local import LocalCommandLineCodeExecutor
from autogen_ext.code_executors.docker import DockerCommandLineCodeExecutor
from autogen_agentchat.tools import AgentTool, TeamTool
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.models.ollama import OllamaChatCompletionClient
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
import configparser
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
from typing import Annotated, Literal, List, Optional, Union, Sequence, Any, Callable, Dict, Mapping, AsyncGenerator
from datetime import datetime, timedelta
from getpass import getpass
import asyncio
import nest_asyncio
from neo4j import GraphDatabase
from minio import Minio
from minio.error import S3Error

# Create a single global event loop for all requests
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

# OpenAI version - if key is available:
config = configparser.ConfigParser(allow_no_value = True)
config.read('openaiapi.ini')
openai_api_key = config.get('openai', 'OPENAI_API_KEY')

openai_model_client = OpenAIChatCompletionClient(
    model = "gpt-4o",
    api_key = openai_api_key,
)

# Must disable parallel tool calls to avoid concurrency issues in AgentTool/TeamTool
openai_model_client_no_parallel_calls = OpenAIChatCompletionClient(
    model = "gpt-4o",
    api_key = openai_api_key,
    parallel_tool_calls=False,  
)

# Assuming Ollama server is running locally on port 11434:
ollama_model_client = OllamaChatCompletionClient(model="llama3.1:8b", host= "http://llm:11434")

# All agents get following config. Change LLM config to experiment:
current_model_client = openai_model_client

# Database Schema (METAMODEL)
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
        - type: string ("input"|"config"|"output"|"action"|"storage"|"api")
        - label: string 
        - description: string
        - content: string 
        - has_files: string ("yes"|"no") - default: "no"
        - endpoint: string
        - database: string - default : "minio"
        - param: string
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

app = Flask(__name__)

# Define a function to set the CORS headers
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = 'http://localhost:8080'  # allowed origin
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'  # Adjust as needed
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
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

async def _generate_dockerfiles_with_agent(filenames: list[str]) -> ListDockerfilesResponse:
    sh_files = [fn for fn in filenames if fn.lower().endswith(".sh")]
    dockerfile_generator = AssistantAgent(
        name="dockerfile_generator",
        model_client=current_model_client,
        description="An agent that generates Dockerfiles.",
        system_message=f""" You will be given a list of files. Only consider files with .sh extension. Generate a Dockerfile per .sh file by replacing <insert filename> with the actual filename in the content below and name the Dockerfile.<insert filename with no extension>:
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

    result = await dockerfile_generator.run(task="List of files: " + str(sh_files))
    print("[analytics_api.py] Dockerfile generator response:")
    print(result.messages[-1].content)
    return result.messages[-1].content

@app.route('/agentic_generate_dockerfiles', methods=['POST', 'OPTIONS'])
def agentic_generate_dockerfiles():
    # Preflight
    if request.method == 'OPTIONS':
        return make_response("", 200)
    data = request.get_json() or {}
    files = data.get("files", [])
    filenames = [f["filename"] for f in files]
    #buckets = [f["bucket"] for f in files]
    print("[analytics_api.py] Filenames received:", filenames)
    #print("[analytics_api.py] Buckets received:", buckets)
    try:
        parsed: ListDockerfilesResponse = run_async(_generate_dockerfiles_with_agent(filenames))
        return jsonify(parsed.model_dump()), 200
    except Exception as e:
        print("[analytics_api.py] Error generating dockerfiles:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/test_agentic_generate_dockerfiles', methods=['POST', 'OPTIONS'])
def test_agentic_generate_dockerfiles():
    # Preflight
    if request.method == 'OPTIONS':
        return make_response("", 200)
    data = request.get_json() or {}
    files = data.get("files", [])
    filenames = [f["filename"] for f in files]
    buckets = [f["bucket"] for f in files]
    print("[analytics_api.py] Filenames received:", filenames)
    print("[analytics_api.py] Bucket received:", buckets)
    # TODO: Agent to generate Dockerfiles:
    # Dummy test response for now
    return jsonify({
        "dockerfiles": [
            {
                "dockerfile": f"# Dockerfile for {f['filename']} in {f['bucket']}"
            }
            for f in files
        ]
    })

@app.route('/agentic_generate_yaml', methods=['POST', 'OPTIONS'])
def agentic_generate_yaml(): 
    # Preflight
    if request.method == 'OPTIONS':
        return make_response("", 200)
    data = request.get_json() or {}
    dockerfile_json = data.get("dockerfile_json")
    print("[analytics_api.py] Dockerfile received:", dockerfile_json)
    # TODO:
    # Agent to fetch full pipeline overview
    # Agent to read files
    # Agent to Generate YAML file
    yaml_text = "YAML"  # replace with real YAML later
    resp = make_response(yaml_text, 200)
    resp.headers["Content-Type"] = "application/x-yaml; charset=utf-8"
    return resp

@app.route('/agentic_pipeline_editor', methods=['GET'])
def agentic_pipeline_editor():
    task = request.args.get('task')
    # User proxy:
    user_proxy = UserProxyAgent("user_proxy")
    async def run_query(query: str, query_type: str) -> str:
        """ Runs any Cypher query in Neo4j. Returns String representation. """
        try:
            api_url = "http://localhost:5001/neo4j_run_query"  # Adjust if API host differs
            payload = {"query": query}
            headers = {"Content-Type": "application/json"}
            # Run the API call in a thread (since requests is blocking)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(api_url, data=json.dumps(payload), headers=headers)
            )
            # Check response status and return:
            if response.status_code == 200:
                records = response.text 
                return repr(records) + f"<({query_type})>"
            else:
                return repr({"Error in graph_operator": f"{response.status_code} - {response.text}"})
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
    # TODO: Use later
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

    # TODO: Use later
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

    # TODO: Use later
    async def inspect_step(step_uid: str) -> str:
        """ Inspects a step: returns incoming/outgoing neighbors and used files."""
        try:
            query_type = "inspect_step"
            query = f"""
            MATCH (s:STEP {{uid: '{step_uid}'}})
            OPTIONAL MATCH (prev:STEP)-[rin:FLOWS_TO]->(s)
            OPTIONAL MATCH (s)-[rout:FLOWS_TO]->(next:STEP)
            OPTIONAL MATCH (s)-[:USES_FILE]->(f:FILE)
            RETURN s,
            collect(DISTINCT {{prev: prev, rel: rin}})  AS incoming_neighbors,
            collect(DISTINCT {{next: next, rel: rout}}) AS outgoing_neighbors,
            collect(DISTINCT f)                         AS used_files;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})
        
    async def overview() -> str:
        """Gives an overview of all the pipelines and the steps present in each pipeline."""
        try:
            query_type = "overview"
            query = """
            MATCH (p:PIPELINE)
            OPTIONAL MATCH (p)-[hs:HAS_STEP]->(s:STEP)
            OPTIONAL MATCH (s)-[r:FLOWS_TO]->(t:STEP)
            RETURN
            p  AS pipeline,
            s  AS step,
            hs AS step_link,
            hs.order_index AS step_order,
            r  AS flow,
            t  AS next_step
            ORDER BY p.name, hs.order_index;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})

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
            RETURN p;
            """

            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})

    async def create_step(params: str) -> str:
        """ Creates new STEP and connects it after the last STEP (if any is present).
        params JSON:
        {
        "name": "<string>",
        "description": "<string>",
        "type": "action|input|output|config|storage|api",
        }
        """
        try:
            query_type = "create_step"
            data = json.loads(params)
            step_type   = data["type"].replace("'", "\\'")
            name        = data.get("name", "").replace("'", "\\'")
            description = data.get("description", "").replace("'", "\\'")
            step_type_lower = step_type.lower()
            props_lines = [
                "uid:        randomUUID()",
                f"type:       '{step_type}'",
                f"name:       '{name}'",
                f"description:'{description}'"
            ]
            if step_type_lower == "input":
                props_lines.append("content: ''")
                props_lines.append("has_files: 'no'")
            elif step_type_lower == "config":
                props_lines.append("param: {}")
            elif step_type_lower == "action":
                props_lines.append("has_files: 'no'")
            elif step_type_lower == "storage":
                props_lines.append("endpoint: ''")
                props_lines.append("database: 'minio'")
            props_str = ",\n            ".join(props_lines)

            query = f"""
            MATCH (p:PIPELINE)
            OPTIONAL MATCH (p)-[hs:HAS_STEP]->(prev:STEP)
            WITH p, prev, hs
            ORDER BY hs.order_index DESC
            LIMIT 1
            WITH
            p,
            prev,
            coalesce(hs.order_index, 0) AS maxIndex
            CREATE (s:STEP {{
                {props_str}
            }})
            MERGE (p)-[:HAS_STEP {{
            order_index: maxIndex + 1
            }}]->(s)
            FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
            MERGE (prev)-[:FLOWS_TO]->(s)
            )
            RETURN s;
            """

            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})

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
            OPTIONAL MATCH (s)<-[hs:HAS_STEP]-(:PIPELINE)
            DELETE hs
            DETACH DELETE s;
            """

            result = await run_query(query, query_type)
            return repr(result)
        except Exception as e:
            return repr({"Error in graph_operator": str(e)})

    pipeline_editor = AssistantAgent(
        name = "pipeline_editor",
        model_client = current_model_client,
        tools = [create_pipeline, create_step, delete_step, overview], 
        description = f"An agent that designs AI/data pipelines given a user request.",
        system_message = f""" You design AI/data pipelines using your registered tools. Call one or multiple tools to create or modify a pipeline as requested by the user.
                          A PIPELINE is composed of one or several STEPs. Use overview to check if there are any pipelines. If the user request is unclear or incomplete, ask for more details.
                        - [overview]: calling this tool will give you an overview of the current pipeline content, if any. 
                        - [create_pipeline]: calling this tool will create a pipeline.
                        - [create_step]: calling this tool will create a new step in a pipeline (will always place it last).
                        - [delete_step]: calling this tool will delete a step in a pipeline.
                          """,  
        max_tool_iterations = 15,
        reflect_on_tool_use = False,
    )

    # TODO: Align later for FILE fetching functionality:
    #async def readFile(bucket: str) -> str:
    #    """Reads the file from a bucket."""
    #    try:
    #        response = requests.get(
    #            "http://localhost:5000/minio_lumen_download",
    #            params={
    #                'endpoint': bucket
    #            }
    #        )
    #        if response.ok:
    #            json_response = response.json()
    #            # print(json_response)
    #            file_path = json_response["file_path"]
    #            mime = magic.Magic(mime=True) 
    #            file_type = mime.from_file("./downloads/" + file_path)
    #            print(f"[MinIO] filepath_driver saved static data to path: {file_path}, MIME type: {file_type}")
    #            return "File path: "+file_path+" | File type: "+file_type
    #        print(f"[MinIO] filepath_driver HTTP error <(BUCKET_ERROR)> {response.status_code}: {response.text}")
    #        return f"[MinIO] filepath_driver HTTP error <(BUCKET_ERROR)> {response.status_code}: {response.text}"
    #    except Exception as e:
    #        print(f"[MinIO] Error in filepath_driver <(BUCKET_ERROR)>: {e}")
    #        return f"[MinIO] Error in filepath_driver <(BUCKET_ERROR)>: {e}"

    #read_file_tool = FunctionTool(readFile, description="Reads the file from the bucket in which it is stored.")
    
     #__________________________________File Reader__________________________________________
    # TODO: Align
    #file_reader = ForcedAssistantAgent(
    #    name = "file_reader",
    #    model_client = current_model_client,
    #    tools = [read_file_tool],
    #    description = "An agent that reads the file(s) associated with a pipeline step. ",
    #    system_message = """<TODO>""",
    #    max_tool_iterations = 1,
    #    reflect_on_tool_use = False 
    #)

    #__________________________________Code Generator__________________________________________
    # TODO: Align
    #executor =  LocalCommandLineCodeExecutor(timeout = 600, work_dir = llm_work_dir)
    #execute_code = PythonCodeExecutionTool(executor) # Tool that executes Python code 
    # Agent that generates and executes tests (monitored version)
    #code_generator = ForcedAssistantAgent(
    #    name = "code_generator",
    #    model_client = current_model_client,
    #    tools = [execute_code],
    #    description = "An agent that generates Python code to print the file content and executes it via registered tool. ",
    #    system_message = "Generate Python code to <TODO>. Call main() at the end. Only output the code. No structural assumptions. Pre-installed packages: tabulate, numpy, scapy, pandas, matplotlib, dpkt (for PCAP files). ",
    #    max_tool_iterations = 1,
    #    model_context=BufferedChatCompletionContext(buffer_size=1)
    #)

    # Team of agents
    outer_termination = TextMentionTermination("exit", sources=["user_proxy"])
    max_messages_termination = MaxMessageTermination(max_messages = 25)
    termination = outer_termination | max_messages_termination

    def selector_func(messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
        if len(messages) == 0:
            return "user_proxy"
        if messages[-1].source == "user_proxy":
            return "pipeline_editor"
        if messages[-1].source == "pipeline_editor":
            # TODO: Align the KEYs accordingly.
            if "<(TODO)>" in messages[-1].to_text():
                print("TODO")
            return user_proxy
        # TODO: Add agents that remain
        return None
    
    # Create the group chat
    selector_team = SelectorGroupChat(
        [user_proxy, pipeline_editor],
        model_client=current_model_client,
        selector_func=selector_func,
        allow_repeated_speaker=True,
        termination_condition=termination
    )

    async def run_team():
        await selector_team.reset()
        # await executor.start()
        result_text = ""
        async for event in selector_team.run_stream(task="I want to design a pipeline."):
            if hasattr(event, "source") and getattr(event, "source", "") == "output_repeater":
                print("[analytics_inlumen.py] Message Source: " + getattr(event, "source", ""))
                print(event.to_text())
                result_text += event.to_text()
            else:
                if isinstance(event, BaseChatMessage) or isinstance(event, BaseAgentEvent):
                    if hasattr(event, "source"):
                        print("[analytics_inlumen.py] Message Source: " + getattr(event, "source", ""))
                    else:
                        print("[analytics_inlumen.py] Message Type: EVENT")
                    print(event.to_text())
        # await executor.stop()
        await selector_team.reset()
        return result_text

    # Run async safely
    result = run_async(run_team())

    # Cleanup result
    response_content = {"result": result}
    return jsonify(response_content)


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5002)