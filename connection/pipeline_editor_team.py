import json
from typing import Any, AsyncGenerator, List, Optional, Sequence, Union

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.messages import ModelClientStreamingChunkEvent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core import CancellationToken
from autogen_core.model_context import ChatCompletionContext
from autogen_core.models import ChatCompletionClient, CreateResult, SystemMessage
from autogen_core.tools import BaseTool, Workbench
from pydantic import BaseModel

from graph_client import run_neo4j_query
from llm_config import LLMConfig, log_llm_selection, select_model_client
from step_types import normalize_step_type


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
        all_messages = await model_context.get_messages()
        llm_messages = cls._get_compatible_context(
            model_client=model_client,
            messages=system_messages + all_messages,
        )
        tools = [tool for wb in workbench for tool in await wb.list_tools()] + handoff_tools
        if model_client_stream:
            model_result: Optional[CreateResult] = None
            async for chunk in model_client.create_stream(
                llm_messages,
                tools=tools,
                tool_choice="required",
                json_output=output_content_type,
                cancellation_token=cancellation_token,
            ):
                if isinstance(chunk, CreateResult):
                    model_result = chunk
                elif isinstance(chunk, str):
                    yield ModelClientStreamingChunkEvent(
                        content=chunk,
                        source=agent_name,
                        full_message_id=message_id,
                    )
                else:
                    raise RuntimeError(f"Invalid chunk type: {type(chunk)}")
            if model_result is None:
                raise RuntimeError("No final model result in streaming mode.")
            yield model_result
        else:
            model_result = await model_client.create(
                llm_messages,
                tools=tools,
                tool_choice="required",
                cancellation_token=cancellation_token,
                json_output=output_content_type,
            )
            yield model_result


def build_pipeline_editing_team(
    llm_config: LLMConfig,
    neo4j_api_base_url: str,
) -> RoundRobinGroupChat:
    log_llm_selection("Building pipeline editing team", llm_config)
    model_client = select_model_client(llm_config)

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
    _ = DB_SCHEMA

    async def run_query(query: str, query_type: str) -> str:
        """Run a Cypher query against Neo4j and return results."""
        return await run_neo4j_query(neo4j_api_base_url, query, query_type)

    async def list_pipelines() -> str:
        """Lists all pipelines and the number of steps they have."""
        try:
            query_type = "list_pipelines"
            query = """
            MATCH (p:PIPELINE)
            OPTIONAL MATCH (p)-[:HAS_STEP]->(s:STEP)
            RETURN p, count(DISTINCT s) AS step_count
            ORDER BY p.name;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as exc:
            return repr({"Error in graph_operator": str(exc)})

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
        except Exception as exc:
            return repr({"Error in graph_operator": str(exc)})

    async def inspect_step(step_uid: str) -> str:
        """Inspects a step: returns incoming/outgoing neighbors and used files."""
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
        except Exception as exc:
            return repr({"Error in graph_operator": str(exc)})

    async def overview() -> str:
        """Gives an overview of the pipeline, the present steps and linked files."""
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
            return repr(result)
        except Exception as exc:
            return repr({"Error in graph_operator": str(exc)})

    async def create_pipeline(params: str) -> str:
        """Creates a PIPELINE node."""
        try:
            query_type = "create_pipeline"
            data = json.loads(params)
            name = data.get("name", "").replace("'", "\\'")
            description = data.get("description", "").replace("'", "\\'")
            version = str(data.get("version", "1.0")).replace("'", "\\'")
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
        except Exception as exc:
            return repr({"Error in graph_operator": str(exc)})

    async def create_step(params: str) -> str:
        """Creates new STEP and connects it after the last STEP, if present."""
        try:
            query_type = "create_step"
            data = json.loads(params)
            step_type = normalize_step_type(data.get("type"))
            step_type_lower = step_type
            label = str(data.get("label", "")).replace("'", "\\'")
            description = str(data.get("description", "")).replace("'", "\\'")
            props_lines = [
                "uid:        randomUUID()",
                f"type:       '{step_type}'",
                f"label:      '{label}'",
                f"description:'{description}'",
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
        except Exception as exc:
            return repr({"Error in graph_operator": str(exc)})

    async def delete_step(params: str) -> str:
        """Deletes a STEP."""
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
        except Exception as exc:
            return repr({"Error in graph_operator": str(exc)})

    user_proxy = UserProxyAgent("user_proxy")
    _ = user_proxy

    pipeline_editor = AssistantAgent(
        name="pipeline_editor",
        model_client=model_client,
        tools=[create_pipeline, create_step, delete_step, overview],
        description="An agent that designs AI/data pipelines given a user request.",
        system_message=""" You design AI/data pipelines using your registered tools. Call one or multiple tools to create or modify a pipeline as requested by the user.
                          A PIPELINE is composed of one or several STEPs. Use overview to check if there are any pipelines. If the user request is unclear or incomplete, ask for more details.
                        - [overview]: calling this tool will give you an overview of the current pipeline content, if any. 
                        - [create_pipeline]: calling this tool will create a pipeline. 
                        - [create_step]: calling this tool will create a new step in a pipeline (will always place it last).
                        - [delete_step]: calling this tool will delete a step in a pipeline.
                        Tool calls MUST use a single string argument named params. The value of params MUST be a JSON-encoded string matching the "params JSON" schema in the docstring.
                        The create_step type MUST be one of: input, action, output, config, storage, api, custom.
                        Use the label/description fields for domain-specific names such as ingestion, preprocessing, model training, or alerting.
                        """,
        max_tool_iterations=10,
        reflect_on_tool_use=True,
    )

    return RoundRobinGroupChat([pipeline_editor], max_turns=1)
