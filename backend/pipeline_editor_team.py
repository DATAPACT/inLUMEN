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
    authorization: str | None = None,
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
        return await run_neo4j_query(query, query_type, authorization=authorization)

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
        """Creates or updates the current design PIPELINE and its active PIPELINE_VERSION.

        params JSON:
        {
          "name": "pipeline name",
          "description": "1-2 sentence pipeline description",
          "version": "optional version name"
        }
        """
        try:
            query_type = "create_pipeline"
            data = json.loads(params)
            name = data.get("name", "").replace("'", "\\'")
            description = data.get("description", "").replace("'", "\\'")
            version = str(data.get("version", "")).replace("'", "\\'")
            query = f"""
            OPTIONAL MATCH (candidate:PIPELINE {{status:'design'}})
            OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
            WITH candidate, count(candidateStep) AS step_count
            ORDER BY step_count DESC, candidate.updated_at DESC
            WITH collect(candidate)[0] AS existing, count(candidate) AS design_pipeline_count
            CALL {{
              WITH existing
              WITH existing WHERE existing IS NULL
              CREATE (p:PIPELINE {{
                uid:        randomUUID(),
                name:       '{name}',
                label:      '{name}',
                description:'{description}',
                version:    CASE WHEN '{version}' <> '' THEN '{version}' ELSE 'Main' END,
                active_version_uid: 'main',
                created_at: datetime(),
                updated_at: datetime(),
                status:     'design'
              }})
              RETURN p, true AS created

              UNION

              WITH existing
              WITH existing WHERE existing IS NOT NULL
              SET existing.name = CASE WHEN '{name}' <> '' THEN '{name}' ELSE coalesce(existing.name, existing.label, '') END,
                  existing.label = CASE WHEN '{name}' <> '' THEN '{name}' ELSE coalesce(existing.label, existing.name, '') END,
                  existing.description = CASE WHEN '{description}' <> '' THEN '{description}' ELSE coalesce(existing.description, '') END,
                  existing.version = CASE WHEN '{version}' <> '' THEN '{version}' ELSE coalesce(existing.version, 'Main') END,
                  existing.updated_at = datetime()
              RETURN existing AS p, false AS created
            }}
            WITH p, created, design_pipeline_count,
                coalesce(p.active_version_uid, 'main') AS activeVersionUid
            WITH p, created, design_pipeline_count, activeVersionUid,
                CASE WHEN activeVersionUid = 'main' THEN 'Main' ELSE coalesce(p.version, 'Main') END AS activeVersionName
            MERGE (v:PIPELINE_VERSION {{uid: activeVersionUid}})
            ON CREATE SET v.created_at = datetime(),
                          v.version_index = CASE WHEN activeVersionUid = 'main' THEN 0 ELSE null END,
                          v.is_main = CASE WHEN activeVersionUid = 'main' THEN true ELSE false END
            SET v.name = activeVersionName,
                v.version = activeVersionName,
                v.description = coalesce(p.description, ''),
                v.updated_at = datetime()
            MERGE (p)-[:HAS_VERSION]->(v)
            SET p.active_version_uid = activeVersionUid,
                p.version = activeVersionName,
                p.description = v.description,
                p.updated_at = datetime()
            RETURN {{
            uid: p.uid,
            name: p.name,
            label: p.label,
            description: p.description,
            version: p.version,
            active_version_uid: p.active_version_uid,
            active_version_name: v.name,
            active_version_description: v.description,
            status: p.status,
            created: created,
            design_pipeline_count: design_pipeline_count,
            created_at: toString(p.created_at),
            updated_at: toString(p.updated_at)
            }} AS pipeline;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as exc:
            return repr({"Error in graph_operator": str(exc)})

    def _step_props_lines(step_type: str, label: str, description: str) -> List[str]:
        """Builds shared STEP properties for create and insert tools."""
        props_lines = [
            "uid:        randomUUID()",
            f"type:       '{step_type}'",
            f"label:      '{label}'",
            f"description:'{description}'",
        ]
        if step_type == "input":
            props_lines.append("content: ''")
            props_lines.append("has_files: 'no'")
        elif step_type == "config":
            props_lines.append("param_json: '{}'")
        elif step_type == "action":
            props_lines.append("has_files: 'no'")
        elif step_type == "storage":
            props_lines.append("endpoint: ''")
            props_lines.append("database: 'minio'")
        elif step_type == "api":
            props_lines.append("endpoint: ''")
        elif step_type == "output":
            props_lines.append("content: ''")
            props_lines.append("has_files: 'no'")
        elif step_type == "custom":
            props_lines.append("has_files: 'no'")
        return props_lines

    async def create_step(params: str) -> str:
        """Creates new STEP and connects it after the last STEP, if present."""
        try:
            query_type = "create_step"
            data = json.loads(params)
            step_type = normalize_step_type(data.get("type"))
            label = str(data.get("label", "")).replace("'", "\\'")
            description = str(data.get("description", "")).replace("'", "\\'")
            props_lines = _step_props_lines(step_type, label, description)
            props_str = ",\n            ".join(props_lines)
            query = f"""
            OPTIONAL MATCH (candidate:PIPELINE {{status:'design'}})
            OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
            WITH candidate, count(candidateStep) AS step_count
            ORDER BY step_count DESC, candidate.updated_at DESC
            WITH collect(candidate)[0] AS candidate
            CALL {{
              WITH candidate
              WITH candidate WHERE candidate IS NULL
              CREATE (p:PIPELINE {{
                uid:        randomUUID(),
                name:       '',
                label:      '',
                description:'',
                version:    '1.0',
                created_at: datetime(),
                updated_at: datetime(),
                status:     'design'
              }})
              RETURN p

              UNION

              WITH candidate
              WITH candidate WHERE candidate IS NOT NULL
              RETURN candidate AS p
            }}
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

    async def insert_step(params: str) -> str:
        """Inserts a STEP before an existing STEP.

        params JSON:
        {
          "type": "input|action|output|config|storage|api|custom",
          "label": "step label",
          "description": "step description",
          "before_flow_id": "required target flow_id",
          "after_flow_id": "optional source flow_id for between-step insertion"
        }

        Modes:
        - Between directly connected steps: pass after_flow_id and before_flow_id.
          Rewires after -> before into after -> new -> before.
        - Initial step insertion: pass only before_flow_id. The target must have
          no incoming FLOWS_TO edge, then the tool creates new -> before.
        The target step and every downstream step are shifted 300px right before
        the new step is placed at the target's previous canvas position.
        """
        try:
            data = json.loads(params)
            step_type = normalize_step_type(data.get("type"))
            label = str(data.get("label", "")).replace("'", "\\'")
            description = str(data.get("description", "")).replace("'", "\\'")
            before_flow_id = str(data["before_flow_id"]).replace("'", "\\'")
            raw_after_flow_id = data.get("after_flow_id")
            after_flow_id = (
                str(raw_after_flow_id).replace("'", "\\'")
                if raw_after_flow_id is not None and str(raw_after_flow_id).strip() != ""
                else None
            )
            props_str = ",\n            ".join(_step_props_lines(step_type, label, description))

            if after_flow_id is None:
                query_type = "insert_initial_step"
                query = f"""
                MATCH (p:PIPELINE)-[:HAS_STEP]->(before:STEP {{flow_id: '{before_flow_id}'}})
                OPTIONAL MATCH (:STEP)-[incoming:FLOWS_TO]->(before)
                WITH p, before, count(incoming) AS incoming_count,
                    coalesce(before.x, 0.0) AS insertX,
                    coalesce(before.y, 0.0) AS insertY
                WHERE incoming_count = 0
                OPTIONAL MATCH (before)-[:FLOWS_TO*0..]->(downstream:STEP)
                WITH p, before, insertX, insertY, collect(DISTINCT downstream) AS downstreamSteps
                FOREACH (node IN downstreamSteps |
                    SET node.x = coalesce(node.x, 0.0) + 300.0
                )
                WITH p, before, insertX, insertY, downstreamSteps
                OPTIONAL MATCH (sAll:STEP)
                WHERE sAll.flow_id IS NOT NULL AND toString(sAll.flow_id) =~ '^[0-9]+$'
                WITH p, before, insertX, insertY, downstreamSteps,
                    coalesce(max(toInteger(sAll.flow_id)), 0) + 1 AS nextFlowId
                CREATE (s:STEP {{
                uid: randomUUID(),
                {props_str},
                flow_id: toString(nextFlowId),
                x: insertX,
                y: insertY
                }})
                MERGE (p)-[:HAS_STEP]->(s)
                MERGE (s)-[:FLOWS_TO]->(before)
                SET p.updated_at = datetime()
                RETURN {{
                mode: 'initial',
                flow_id: s.flow_id,
                uid: s.uid,
                type: s.type,
                label: s.label,
                description: s.description,
                x: s.x,
                y: s.y,
                before_flow_id: before.flow_id,
                shifted_flow_ids: [node IN downstreamSteps | node.flow_id],
                pipeline_updated_at: toString(p.updated_at)
                }} AS step;
                """
            else:
                query_type = "insert_between_steps"
                query = f"""
                MATCH (p:PIPELINE)-[:HAS_STEP]->(before:STEP {{flow_id: '{before_flow_id}'}})
                MATCH (p)-[:HAS_STEP]->(after:STEP {{flow_id: '{after_flow_id}'}})
                MATCH (after)-[oldFlow:FLOWS_TO]->(before)
                WITH p, after, before, oldFlow,
                    coalesce(before.x, 0.0) AS insertX,
                    coalesce(before.y, 0.0) AS insertY
                OPTIONAL MATCH (before)-[:FLOWS_TO*0..]->(downstream:STEP)
                WITH p, after, before, oldFlow, insertX, insertY, collect(DISTINCT downstream) AS downstreamSteps
                FOREACH (node IN downstreamSteps |
                    SET node.x = coalesce(node.x, 0.0) + 300.0
                )
                WITH p, after, before, oldFlow, insertX, insertY, downstreamSteps
                OPTIONAL MATCH (sAll:STEP)
                WHERE sAll.flow_id IS NOT NULL AND toString(sAll.flow_id) =~ '^[0-9]+$'
                WITH p, after, before, oldFlow, insertX, insertY, downstreamSteps,
                    coalesce(max(toInteger(sAll.flow_id)), 0) + 1 AS nextFlowId
                DELETE oldFlow
                CREATE (s:STEP {{
                uid: randomUUID(),
                {props_str},
                flow_id: toString(nextFlowId),
                x: insertX,
                y: insertY
                }})
                MERGE (p)-[:HAS_STEP]->(s)
                MERGE (after)-[:FLOWS_TO]->(s)
                MERGE (s)-[:FLOWS_TO]->(before)
                SET p.updated_at = datetime()
                RETURN {{
                mode: 'between',
                flow_id: s.flow_id,
                uid: s.uid,
                type: s.type,
                label: s.label,
                description: s.description,
                x: s.x,
                y: s.y,
                after_flow_id: after.flow_id,
                before_flow_id: before.flow_id,
                shifted_flow_ids: [node IN downstreamSteps | node.flow_id],
                pipeline_updated_at: toString(p.updated_at)
                }} AS step;
                """
            result = await run_query(query, query_type)
            return repr(result)
        except KeyError:
            return repr({"Error in graph_operator": "insert_step requires before_flow_id"})
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

    async def delete_all_steps(params: str) -> str:
        """Deletes all STEPs from the current design pipeline while keeping the PIPELINE node.

        params JSON: {}
        """
        try:
            query_type = "delete_all_steps"
            _ = json.loads(params) if params else {}

            query = """
            OPTIONAL MATCH (candidate:PIPELINE {status:'design'})
            OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
            WITH candidate, count(candidateStep) AS step_count
            ORDER BY step_count DESC, candidate.updated_at DESC
            WITH collect(candidate)[0] AS p
            WHERE p IS NOT NULL
            OPTIONAL MATCH (p)-[:HAS_STEP]->(s:STEP)
            WITH p, collect(DISTINCT s) AS steps
            WITH p, steps, size(steps) AS deleted_step_count
            CALL {
              WITH steps
              UNWIND steps AS step
              DETACH DELETE step
              RETURN count(*) AS deleted_rows
            }
            SET p.updated_at = datetime()
            RETURN {
            pipeline_uid: p.uid,
            pipeline_label: coalesce(p.label, p.name, ''),
            deleted_step_count: deleted_step_count,
            pipeline_updated_at: toString(p.updated_at)
            } AS pipeline;
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
        tools=[create_pipeline, create_step, insert_step, delete_step, delete_all_steps, overview],
        description="An agent that designs AI/data pipelines given a user request.",
        system_message=""" You design AI/data pipelines using your registered tools. Call one or multiple tools to create or modify a pipeline as requested by the user.
                          A PIPELINE is composed of one or several STEPs. Use overview to check if there are any pipelines. If the user request is unclear or incomplete, ask for more details.
                        - [overview]: calling this tool will give you an overview of the current pipeline content, if any.
                        - [create_pipeline]: calling this tool will create a pipeline.
                        - [create_step]: calling this tool will create a new step in a pipeline (will always place it last).
                        - [insert_step]: calling this tool will insert a new step before an existing step. Use it instead of create_step when the user asks to add a step between existing steps (for example, "add preprocessing between ingestion and training") or as a new initial step (for example, "add an initial validation step"). For between-step insertion, pass after_flow_id and before_flow_id for directly connected steps. For initial insertion, pass only before_flow_id; the target must currently have no incoming FLOWS_TO edge.
                        - [delete_step]: calling this tool will delete a step in a pipeline.
                        - [delete_all_steps]: calling this tool will remove every step from the current design pipeline while keeping the pipeline itself. Use it only when the user asks to clear, empty, reset, delete all steps, remove everything from, or remove all nodes/steps in the pipeline.
                        Tool calls MUST use a single string argument named params. The value of params MUST be a JSON-encoded string matching the "params JSON" schema in the docstring.
                        When creating, designing, regenerating, or rebuilding a pipeline, call create_pipeline first with a concise generated name and a fresh 1-2 sentence description that summarizes the full intended pipeline. Do this before creating steps so the UI pipeline description is updated.
                        The create_step and insert_step type MUST be one of: input, action, output, config, storage, api, custom.
                        Use overview to find the relevant flow_id values before calling insert_step unless the flow_id values are already provided by the user.
                        Use the label/description fields for domain-specific names such as ingestion, preprocessing, model training, or alerting.
                        """,
        max_tool_iterations=30,
        reflect_on_tool_use=True,
    )

    return RoundRobinGroupChat([pipeline_editor], max_turns=1)
