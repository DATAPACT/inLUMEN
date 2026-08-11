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
from model_plans import resolve_implementation_plan
from node_ports import (
    default_input_port_id,
    normalize_node_ports,
    ports_json,
    ports_json_for_template,
)
from node_parameters import secret_params_json
from pipeline_graph_validation import validate_pipeline_graph
from step_types import normalize_step_type
from subpipeline_reference import (
    derive_subpipeline_interface,
    missing_explicit_port_contracts,
    normalize_reusable_pipeline_graph,
    plan_subpipeline_port_migration,
    public_ports_for_interface,
)


SEMANTIC_IMPLEMENTATION_KIND_ALIASES = {
    "trusted-pretrained-inference": ("generated-code", "trusted_heavy_model"),
    "trusted_pretrained_inference": ("generated-code", "trusted_heavy_model"),
    "classical-ml-training": ("generated-code", "classical_ml"),
    "classical_ml_training": ("generated-code", "classical_ml"),
    "deterministic-processing": ("generated-code", "deterministic"),
    "deterministic_processing": ("generated-code", "deterministic"),
}


def _agent_query_returned_no_rows(result: object) -> bool:
    if isinstance(result, (list, tuple)):
        return len(result) == 0
    if isinstance(result, str):
        try:
            decoded = json.loads(result)
        except (TypeError, ValueError):
            return False
        return isinstance(decoded, list) and len(decoded) == 0
    return False


def normalize_agent_implementation(implementation: Any) -> dict[str, Any]:
    """Keep runtime packaging separate from a task's semantic execution class."""
    candidate = dict(implementation) if isinstance(implementation, dict) else {}
    raw_kind = str(candidate.get("kind") or "").strip().lower()
    alias = SEMANTIC_IMPLEMENTATION_KIND_ALIASES.get(raw_kind)
    if alias:
        candidate["kind"] = alias[0]
        candidate.setdefault("execution_profile", alias[1])
    return candidate


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
    provenance_context: dict | None = None,
) -> RoundRobinGroupChat:
    log_llm_selection("Building pipeline editing team", llm_config)
    # Graph mutations share ordering and flow-id state. Parallel tool calls can
    # deadlock Neo4j and, even when they all succeed, connect steps in an
    # arbitrary order. Keep each agent turn strictly sequential.
    model_client = select_model_client(llm_config, parallel_tool_calls=False)

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
            - type: string ("source"|"task"|"destination"|"flow"|"subpipeline")
              This is the stable structural kind. Templates, business operations,
              static parameters, and implementation technologies are metadata.
            - label: string
            - description: string
            - content: string
            - has_files: string ("yes"|"no") - default: "no"
            - param_json: string - default "{}"
            - secret_params_json: string - names of masked parameter fields
            - ports_json: string - explicit logical inputs and outputs
            - template_label: string
        (:FILE) represents a single file associated with a step. Properties:
            - uid: string (generated via randomUUID)
            - filename: string
            - added_at: datetime
            - bucket: string
            - role: string ("code"|"data")
        Relationships:
        (:PIPELINE)-[:HAS_STEP]->(:STEP)
        (:STEP)-[:FLOWS_TO]->(:STEP)
        (:STEP)-[:HAS_FILE]->(:FILE)
    """
    _ = DB_SCHEMA

    async def run_query(query: str, query_type: str) -> str:
        """Run a Cypher query against Neo4j and return results."""
        return await run_neo4j_query(
            query,
            query_type,
            authorization=authorization,
            provenance_context=provenance_context,
        )

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
            raise RuntimeError(f"list_pipelines failed: {exc}") from exc

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
            raise RuntimeError(f"get_pipeline_steps failed: {exc}") from exc

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
            raise RuntimeError(f"inspect_step failed: {exc}") from exc

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
            raise RuntimeError(f"overview failed: {exc}") from exc

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
            raise RuntimeError(f"create_pipeline failed: {exc}") from exc

    def _step_props_lines(
        step_type: str,
        label: str,
        description: str,
        template_label: str = "",
        implementation: Any = None,
        parameters: Any = None,
        secret_parameters: Any = None,
    ) -> List[str]:
        """Builds shared STEP properties for create and insert tools."""
        default_template_labels = {
            "source": "Source",
            "task": "Blank Task",
            "destination": "Destination",
            "flow": "Condition",
            "subpipeline": "Subpipeline",
        }
        resolved_template_label = template_label or default_template_labels[step_type]
        param_obj = dict(parameters) if isinstance(parameters, dict) else {}
        if step_type == "flow":
            if resolved_template_label == "Condition":
                expression = str(param_obj.get("expression") or "").strip()
                if not expression:
                    raise ValueError(
                        "Condition Flow requires parameters.expression, for example "
                        "value.sentiment == \"negative\"."
                    )
            elif resolved_template_label == "Parallel Map":
                param_obj.setdefault("max_concurrency", 4)
                param_obj.setdefault("failure_policy", "stop")
            else:
                raise ValueError(
                    "Flow template must be Condition or Parallel Map; generic Flow has no behavior."
                )
        props_lines = [
            f"type:       '{step_type}'",
            f"label:      '{label}'",
            f"description:'{description}'",
            f"template_label:'{resolved_template_label}'",
            "has_files: 'no'",
        ]
        default_ports = ports_json_for_template(
            None,
            step_type,
            resolved_template_label,
        ).replace("\\", "\\\\").replace("'", "\\'")
        props_lines.append(f"ports_json: '{default_ports}'")
        if step_type in ("source", "destination"):
            props_lines.append("content: ''")
        if isinstance(implementation, dict) and implementation:
            implementation = normalize_agent_implementation(implementation)
            implementation = resolve_implementation_plan(
                implementation,
                label=label,
                description=description,
            )
            param_obj["model_plan"] = implementation
        param_json = json.dumps(param_obj, ensure_ascii=True, sort_keys=True)
        escaped_param_json = param_json.replace("\\", "\\\\").replace("'", "\\'")
        props_lines.append(f"param_json: '{escaped_param_json}'")
        secret_json = secret_params_json(secret_parameters, param_obj)
        escaped_secret_json = secret_json.replace("\\", "\\\\").replace("'", "\\'")
        props_lines.append(f"secret_params_json: '{escaped_secret_json}'")
        return props_lines

    def _resolved_template_for_step(
        step_type: str,
        template_label: object,
        label: object,
        description: object,
    ) -> str:
        raw_template = str(template_label or "").strip()
        if step_type != "flow":
            return raw_template
        normalized_template = raw_template.lower()
        if normalized_template == "parallel map":
            return "Parallel Map"
        if normalized_template == "condition":
            return "Condition"
        if raw_template and normalized_template != "flow":
            return raw_template
        behavior_hint = f"{label or ''} {description or ''}".lower()
        if "parallel" in behavior_hint or "for each" in behavior_hint:
            return "Parallel Map"
        return "Condition"

    def _decoded_rows(result: object) -> list[dict[str, Any]]:
        decoded = result
        if isinstance(result, str):
            try:
                decoded = json.loads(result)
            except (TypeError, ValueError):
                return []
        if not isinstance(decoded, list):
            return []
        return [row for row in decoded if isinstance(row, dict)]

    def _cypher_string(value: object) -> str:
        return str(value or "").replace("\\", "\\\\").replace("'", "\\'")

    async def _resolve_subpipeline_for_creation(
        data: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        """Resolve a saved version before a parent Subpipeline node exists."""

        def optional_id(name: str) -> str:
            value = str(data.get(name) or "").strip()
            if value and not all(character.isalnum() or character in "_.-" for character in value):
                raise ValueError(f"create_step received an invalid {name}")
            return value

        pipeline_uid = optional_id("reusable_pipeline_uid")
        version_uid = optional_id("reusable_version_uid")
        pipeline_name = str(data.get("reusable_pipeline_name") or label or "").strip()
        version_name = str(data.get("reusable_version_name") or "").strip()
        if not pipeline_uid and not pipeline_name:
            raise ValueError(
                "Subpipeline creation requires reusable_pipeline_uid or reusable_pipeline_name"
            )

        escaped_pipeline_name = _cypher_string(pipeline_name)
        escaped_version_name = _cypher_string(version_name)
        pipeline_filter = (
            f"rp.uid = '{pipeline_uid}'"
            if pipeline_uid
            else (
                "(toLower(trim(rp.name)) = toLower(trim('"
                f"{escaped_pipeline_name}')) OR "
                "toLower(trim('"
                f"{escaped_pipeline_name}')) CONTAINS toLower(trim(rp.name)))"
            )
        )
        if version_uid:
            version_filter = f"rv.uid = '{version_uid}'"
        elif version_name:
            version_filter = f"toLower(trim(rv.name)) = toLower(trim('{escaped_version_name}'))"
        else:
            version_filter = "rv.uid = rp.active_version_uid"

        lookup = await run_query(f"""
        MATCH (rp:PIPELINE {{status:'reusable'}})-[:HAS_VERSION]->(rv:PIPELINE_VERSION)
        WHERE {pipeline_filter} AND {version_filter}
        RETURN {{
          pipeline_uid:rp.uid, pipeline_name:rp.name,
          version_uid:rv.uid, version_name:rv.name,
          interface_json:rv.interface_json,
          public_ports_json:rv.public_ports_json
        }} AS reusable_pipeline
        ORDER BY CASE WHEN toLower(trim(rp.name)) = toLower(trim('{escaped_pipeline_name}')) THEN 0 ELSE 1 END,
                 rp.name
        LIMIT 2;
        """, "resolve_reusable_pipeline_for_creation")
        matches = [
            row.get("reusable_pipeline")
            for row in _decoded_rows(lookup)
            if isinstance(row.get("reusable_pipeline"), dict)
        ]
        if not matches:
            raise ValueError(
                f"No saved reusable pipeline version matched '{pipeline_name}'. "
                "Call list_reusable_pipelines and use its pipeline/version identifiers."
            )
        if len(matches) > 1 and not pipeline_uid:
            raise ValueError(
                f"Reusable pipeline name '{pipeline_name}' is ambiguous. "
                "Pass reusable_pipeline_uid and reusable_version_uid from list_reusable_pipelines."
            )
        reusable = matches[0]
        try:
            interface = json.loads(reusable.get("interface_json") or "{}")
            public_ports = json.loads(reusable.get("public_ports_json") or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("Saved reusable pipeline version has an invalid public contract") from exc
        inputs = public_ports.get("inputs") if isinstance(public_ports, dict) else None
        outputs = public_ports.get("outputs") if isinstance(public_ports, dict) else None
        input_ids = [
            str(port.get("id") or "")
            for port in inputs or []
            if isinstance(port, dict) and port.get("id")
        ]
        output_ids = [
            str(port.get("id") or "")
            for port in outputs or []
            if isinstance(port, dict) and port.get("id")
        ]
        if not isinstance(interface, dict) or not input_ids or not output_ids:
            raise ValueError(
                "Saved reusable pipeline version must expose at least one public input and output"
            )
        reference = {
            "pipeline_uid": str(reusable.get("pipeline_uid") or ""),
            "pipeline_name": str(reusable.get("pipeline_name") or ""),
            "version_uid": str(reusable.get("version_uid") or ""),
            "version_name": str(reusable.get("version_name") or ""),
        }
        definition = {"version": 2, "reference": reference, "interface": interface}
        return {
            "reference": reference,
            "definition_json": json.dumps(definition, ensure_ascii=True, sort_keys=True),
            "ports_json": ports_json(public_ports, "subpipeline"),
            "input_ids": input_ids,
            "output_ids": output_ids,
        }

    async def create_step(params: str) -> str:
        """Creates new STEP and connects it after the last STEP, if present.

        params JSON:
        {
          "type": "source|task|destination|flow|subpipeline",
          "label": "step label",
          "description": "step description",
          "template": "optional template name such as Speech-to-Text",
          "reusable_pipeline_name": "required for Subpipeline unless uid is supplied",
          "reusable_pipeline_uid": "optional saved reusable pipeline uid",
          "reusable_version_uid": "optional immutable version uid",
          "reusable_version_name": "optional version name; active version is the default",
          "parameters": {"static_parameter": "value"},
          "secret_parameters": ["api_key"],
          "implementation": {
            "kind": "generated-code|python|sql|container|git-repository|rest-api|shell|custom",
            "task": "analytical or model-backed task",
            "domain": "inferred domain",
            "execution_profile": "classical_ml|trusted_heavy_model|custom_model",
            "framework": "optional requested runtime framework",
            "model_id": "only an explicitly requested or registry-known model identifier",
            "model_revision": "optional explicit revision; trusted adapters resolve supported tasks",
            "device": "auto|cpu|cuda",
            "precision": "auto|float32|float16|bfloat16|int8",
            "required_packages": ["package constraints"],
            "inference_parameters": {},
            "selection_rationale": ["why this model fits"]
          }
        }
        Include implementation for analytical steps. For ordinary structured
        model training, specify task/domain and classical_ml but omit model_id.
        Use source/destination templates for external adapters, task templates for
        domain operations, and flow for execution control. Creating a subpipeline
        atomically resolves and pins a saved reusable pipeline version and returns
        its exact public input/output ids; an unreferenced placeholder is never
        created. Never create configuration nodes.
        """
        try:
            query_type = "create_step"
            data = json.loads(params)
            step_type = normalize_step_type(data.get("type"))
            raw_label = str(data.get("label", ""))
            label = _cypher_string(raw_label)
            raw_description = str(data.get("description", ""))
            description = raw_description.replace("'", "\\'")
            resolved_template = _resolved_template_for_step(
                step_type,
                data.get("template"),
                raw_label,
                raw_description,
            )
            props_lines = _step_props_lines(
                step_type,
                label,
                description,
                resolved_template.replace("'", "\\'"),
                data.get("implementation"),
                data.get("parameters"),
                data.get("secret_parameters"),
            )
            resolved_subpipeline = None
            if step_type == "subpipeline":
                resolved_subpipeline = await _resolve_subpipeline_for_creation(data, raw_label)
                props_lines = [
                    line for line in props_lines if not line.startswith("ports_json:")
                ]
                escaped_ports = _cypher_string(resolved_subpipeline["ports_json"])
                escaped_definition = _cypher_string(resolved_subpipeline["definition_json"])
                primary_input = _cypher_string(resolved_subpipeline["input_ids"][0])
                primary_output = _cypher_string(resolved_subpipeline["output_ids"][0])
                props_lines.extend([
                    f"ports_json: '{escaped_ports}'",
                    f"subpipeline_json: '{escaped_definition}'",
                    f"primary_input_port: '{primary_input}'",
                    f"primary_output_port: '{primary_output}'",
                ])
            props_str = ",\n            ".join(props_lines)
            target_port = (
                resolved_subpipeline["input_ids"][0]
                if resolved_subpipeline
                else default_input_port_id(step_type, resolved_template)
            ).replace("'", "\\'")
            subpipeline_return = ""
            if resolved_subpipeline:
                reference = resolved_subpipeline["reference"]
                subpipeline_return = f""",
            referenced_pipeline_uid: '{_cypher_string(reference['pipeline_uid'])}',
            referenced_pipeline_name: '{_cypher_string(reference['pipeline_name'])}',
            referenced_version_uid: '{_cypher_string(reference['version_uid'])}',
            referenced_version_name: '{_cypher_string(reference['version_name'])}',
            public_inputs: {json.dumps(resolved_subpipeline['input_ids'])},
            public_outputs: {json.dumps(resolved_subpipeline['output_ids'])}"""
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
            MERGE (prev)-[flow:FLOWS_TO]->(s)
            SET flow.source_port = CASE
                    WHEN prev.type = 'source' THEN 'data'
                    WHEN prev.type = 'subpipeline' THEN coalesce(prev.primary_output_port, 'output')
                    WHEN prev.type = 'flow' AND toLower(coalesce(prev.template_label, '')) = 'condition' THEN 'when_true'
                    WHEN prev.type = 'flow' AND toLower(coalesce(prev.template_label, '')) = 'parallel map' THEN 'item'
                    ELSE 'output'
                END,
                flow.target_port = '{target_port}'
            )
            RETURN {{
            flow_id: s.flow_id,
            uid: s.uid,
            type: s.type,
            label: s.label,
            description: s.description,
            x: s.x,
            y: s.y,
            pipeline_updated_at: toString(p.updated_at){subpipeline_return}
            }} AS step;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as exc:
            raise RuntimeError(f"create_step failed: {exc}") from exc

    async def connect_steps(params: str) -> str:
        """Creates or configures a port-aware connection between two existing steps.

        params JSON:
        {
          "source_flow_id": "source step flow_id",
          "target_flow_id": "target step flow_id",
          "source_port": "output port id",
          "target_port": "input port id",
          "allow_fan_out": false
        }

        Standard handles are source.data, task.output/input, destination.data,
        Condition.value/when_true/when_false, and Parallel Map.items/item.
        Subpipeline handles are the public port ids returned by create_step.
        For compatibility, input/output aliases are mapped to the pinned
        Subpipeline's primary public ports. Calling this for an existing
        source/target pair updates that connection's handles. Use it to add every
        non-linear branch.
        Flow steps may fan out by design. A non-Flow step may only gain a second
        downstream target when allow_fan_out is explicitly true; omit it for
        ordinary chains and branch merges.
        """
        try:
            data = json.loads(params)

            def required_value(name: str) -> str:
                value = str(data.get(name) or "").strip()
                if not value:
                    raise ValueError(f"connect_steps requires {name}")
                if not all(character.isalnum() or character in "_.-" for character in value):
                    raise ValueError(f"connect_steps received an invalid {name}")
                return value

            source_flow_id = required_value("source_flow_id")
            target_flow_id = required_value("target_flow_id")
            source_port = required_value("source_port")
            target_port = required_value("target_port")
            allow_fan_out = data.get("allow_fan_out") is True
            if source_flow_id == target_flow_id:
                raise ValueError("connect_steps cannot connect a step to itself")

            query = f"""
            MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(source:STEP {{flow_id:'{source_flow_id}'}})
            MATCH (p)-[:HAS_STEP]->(target:STEP {{flow_id:'{target_flow_id}'}})
            WHERE source.type <> 'destination' AND target.type <> 'source'
            OPTIONAL MATCH (source)-[:FLOWS_TO]->(existingTarget:STEP)
            WITH p, source, target, collect(DISTINCT existingTarget) AS existingTargets
            WHERE source.type = 'flow'
               OR {str(allow_fan_out).lower()}
               OR size(existingTargets) = 0
               OR target IN existingTargets
            MERGE (source)-[flow:FLOWS_TO]->(target)
            SET flow.source_port = CASE
                    WHEN source.type = 'subpipeline' AND '{source_port}' = 'output'
                    THEN coalesce(source.primary_output_port, '{source_port}')
                    ELSE '{source_port}'
                END,
                flow.target_port = CASE
                    WHEN target.type = 'subpipeline' AND '{target_port}' = 'input'
                    THEN coalesce(target.primary_input_port, '{target_port}')
                    ELSE '{target_port}'
                END,
                p.updated_at = datetime()
            WITH p, source, target, flow
            OPTIONAL MATCH (source)-[trueConnection:FLOWS_TO]->(trueTarget:STEP)
            WHERE source.type = 'flow'
              AND toLower(coalesce(source.template_label, '')) = 'condition'
              AND trueConnection.source_port = 'when_true'
            OPTIONAL MATCH (source)-[falseConnection:FLOWS_TO]->(falseTarget:STEP)
            WHERE source.type = 'flow'
              AND toLower(coalesce(source.template_label, '')) = 'condition'
              AND falseConnection.source_port = 'when_false'
            OPTIONAL MATCH branchPath = (trueTarget)-[:FLOWS_TO*1..32]->(falseTarget)
            WITH p, source, target, flow, trueTarget, falseTarget,
                count(branchPath) > 0 AS falseTargetIsMerge
            FOREACH (_ IN CASE
                WHEN trueTarget IS NOT NULL AND falseTarget IS NOT NULL THEN [1]
                ELSE []
            END |
                SET trueTarget.y = coalesce(source.y, 0.0) - 180.0,
                    falseTarget.y = CASE
                        WHEN falseTargetIsMerge THEN coalesce(source.y, 0.0)
                        ELSE coalesce(source.y, 0.0) + 180.0
                    END
            )
            RETURN {{
            source_flow_id: source.flow_id,
            source_label: source.label,
            source_port: flow.source_port,
            target_flow_id: target.flow_id,
            target_label: target.label,
            target_port: flow.target_port,
            fan_out_allowed: source.type = 'flow' OR {str(allow_fan_out).lower()},
            pipeline_updated_at: toString(p.updated_at),
            branch_layout_applied: trueTarget IS NOT NULL AND falseTarget IS NOT NULL
            }} AS connection;
            """
            result = await run_query(query, "connect_steps")
            if _agent_query_returned_no_rows(result):
                raise ValueError(
                    "Connection rejected: this non-Flow step already has a different "
                    "downstream target. Remove the shortcut or pass allow_fan_out:true "
                    "only when the user explicitly requested independent consumers."
                )
            return repr(result)
        except Exception as exc:
            raise RuntimeError(f"connect_steps failed: {exc}") from exc

    async def disconnect_steps(params: str) -> str:
        """Deletes one exact port-aware connection between two existing steps.

        params JSON:
        {
          "source_flow_id": "source step flow_id",
          "target_flow_id": "target step flow_id",
          "source_port": "output port id",
          "target_port": "input port id"
        }

        All four fields are required so a repair cannot accidentally remove a
        different branch or connection between the same pair of steps.
        """
        try:
            data = json.loads(params)

            def required_value(name: str) -> str:
                value = str(data.get(name) or "").strip()
                if not value:
                    raise ValueError(f"disconnect_steps requires {name}")
                if not all(character.isalnum() or character in "_.-" for character in value):
                    raise ValueError(f"disconnect_steps received an invalid {name}")
                return value

            source_flow_id = required_value("source_flow_id")
            target_flow_id = required_value("target_flow_id")
            source_port = required_value("source_port")
            target_port = required_value("target_port")
            query = f"""
            MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(source:STEP {{flow_id:'{source_flow_id}'}})
            MATCH (p)-[:HAS_STEP]->(target:STEP {{flow_id:'{target_flow_id}'}})
            MATCH (source)-[flow:FLOWS_TO]->(target)
            WHERE coalesce(flow.source_port, '') = '{source_port}'
              AND coalesce(flow.target_port, '') = '{target_port}'
            WITH p, source, target, collect(flow) AS connections
            FOREACH (connection IN connections | DELETE connection)
            SET p.updated_at = datetime()
            RETURN {{
            source_flow_id: source.flow_id,
            source_label: source.label,
            source_port: '{source_port}',
            target_flow_id: target.flow_id,
            target_label: target.label,
            target_port: '{target_port}',
            deleted_connection_count: size(connections),
            pipeline_updated_at: toString(p.updated_at)
            }} AS disconnected;
            """
            result = await run_query(query, "disconnect_steps")
            if _agent_query_returned_no_rows(result):
                raise ValueError("No connection matched all four identifiers")
            return repr(result)
        except Exception as exc:
            raise RuntimeError(f"disconnect_steps failed: {exc}") from exc

    async def configure_flow_step(params: str) -> str:
        """Configures an existing Flow step with executable behavior and ports.

        params JSON for a condition:
        {
          "flow_id": "existing Flow step flow_id",
          "behavior": "Condition",
          "parameters": {"expression": "value.sentiment == \"negative\""}
        }

        params JSON for a parallel map:
        {
          "flow_id": "existing Flow step flow_id",
          "behavior": "Parallel Map",
          "parameters": {"max_concurrency": 4, "failure_policy": "stop"}
        }

        Existing generic connection handles are migrated to the selected
        behavior. For Condition, use connect_steps afterward to identify every
        requested when_true and when_false branch explicitly.
        """
        try:
            data = json.loads(params)
            flow_id = str(data.get("flow_id") or "").strip()
            if not flow_id or not all(
                character.isalnum() or character in "_.-" for character in flow_id
            ):
                raise ValueError("configure_flow_step requires a valid flow_id")
            behavior = str(data.get("behavior") or "").strip()
            if behavior not in {"Condition", "Parallel Map"}:
                raise ValueError("Flow behavior must be Condition or Parallel Map")
            parameters = (
                dict(data.get("parameters"))
                if isinstance(data.get("parameters"), dict)
                else {}
            )
            if behavior == "Condition" and not str(parameters.get("expression") or "").strip():
                raise ValueError("Condition Flow requires parameters.expression")
            if behavior == "Parallel Map":
                parameters.setdefault("max_concurrency", 4)
                parameters.setdefault("failure_policy", "stop")

            param_json = json.dumps(parameters, ensure_ascii=True, sort_keys=True)
            escaped_param_json = param_json.replace("\\", "\\\\").replace("'", "\\'")
            serialized_ports = ports_json_for_template(None, "flow", behavior)
            escaped_ports = serialized_ports.replace("\\", "\\\\").replace("'", "\\'")
            input_port = default_input_port_id("flow", behavior)
            default_output_port = "when_true" if behavior == "Condition" else "item"
            query = f"""
            MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(flowStep:STEP {{flow_id:'{flow_id}'}})
            WHERE flowStep.type = 'flow'
            SET flowStep.template_label = '{behavior}',
                flowStep.param_json = '{escaped_param_json}',
                flowStep.ports_json = '{escaped_ports}',
                p.updated_at = datetime()
            WITH p, flowStep
            OPTIONAL MATCH (:STEP)-[incoming:FLOWS_TO]->(flowStep)
            WITH p, flowStep, collect(incoming) AS incomingFlows
            OPTIONAL MATCH (flowStep)-[outgoing:FLOWS_TO]->(:STEP)
            WITH p, flowStep, incomingFlows, collect(outgoing) AS outgoingFlows
            FOREACH (connection IN incomingFlows |
                SET connection.target_port = '{input_port}'
            )
            FOREACH (connection IN outgoingFlows |
                SET connection.source_port = CASE
                    WHEN '{behavior}' = 'Condition'
                         AND connection.source_port IN ['when_true', 'when_false']
                    THEN connection.source_port
                    ELSE '{default_output_port}'
                END
            )
            RETURN {{
            flow_id: flowStep.flow_id,
            uid: flowStep.uid,
            label: flowStep.label,
            behavior: flowStep.template_label,
            parameters: flowStep.param_json,
            ports: flowStep.ports_json,
            migrated_incoming_connections: size(incomingFlows),
            migrated_outgoing_connections: size(outgoingFlows),
            pipeline_updated_at: toString(p.updated_at)
            }} AS flow_step;
            """
            result = await run_query(query, "configure_flow_step")
            return repr(result)
        except Exception as exc:
            raise RuntimeError(f"configure_flow_step failed: {exc}") from exc

    async def list_reusable_pipelines(params: str) -> str:
        """Lists separately saved reusable pipelines and their immutable versions.

        params JSON: {}
        """
        _ = json.loads(params) if params else {}
        query = """
        MATCH (p:PIPELINE {status:'reusable'})-[:HAS_VERSION]->(v:PIPELINE_VERSION)
        RETURN {
          pipeline_uid: p.uid,
          pipeline_name: p.name,
          description: p.description,
          active_version_uid: p.active_version_uid,
          version_uid: v.uid,
          version_name: v.name,
          interface_json: v.interface_json,
          node_count: v.node_count,
          edge_count: v.edge_count
        } AS reusable_pipeline
        ORDER BY p.name, v.created_at DESC;
        """
        return repr(await run_query(query, "list_reusable_pipelines"))

    async def create_reusable_pipeline(params: str) -> str:
        """Creates a distinct reusable PIPELINE and immutable version.

        params JSON:
        {
          "name": "Conversation Understanding",
          "description": "Reusable transcription and conversation analysis.",
          "version_name": "Version 1",
          "graph": {"nodes": ["complete React Flow-shaped nodes with explicit typed ports"], "edges": ["port-aware edges"]}
        }

        The graph must be runnable on its own, with Source and Destination
        boundaries. Every node must declare ports.inputs and ports.outputs with
        stable ids and data types; implicit generic ports are rejected. Source
        and Destination ports become the reusable pipeline's public contract.
        """
        try:
            data = json.loads(params)
            name = str(data.get("name") or "").strip()
            description = str(data.get("description") or "").strip()
            version_name = str(data.get("version_name") or "Version 1").strip() or "Version 1"
            graph = data.get("graph") if isinstance(data.get("graph"), dict) else {}
            if not name:
                raise ValueError("create_reusable_pipeline requires name")
            missing_ports = missing_explicit_port_contracts(graph)
            if missing_ports:
                raise ValueError(
                    "Every reusable-pipeline component requires explicit typed ports; missing: "
                    + ", ".join(missing_ports)
                )
            graph = normalize_reusable_pipeline_graph(graph)
            validation = validate_pipeline_graph(graph)
            if not validation.get("valid"):
                messages = [
                    str(issue.get("message") or "Invalid graph")
                    for issue in validation.get("issues", [])
                    if isinstance(issue, dict)
                ]
                raise ValueError("Reusable pipeline graph is invalid: " + "; ".join(messages[:6]))
            interface = derive_subpipeline_interface(graph)
            if not interface["inputs"] or not interface["outputs"]:
                raise ValueError("Reusable pipeline requires Source and Destination boundaries")
            public_ports = public_ports_for_interface(interface)
            escaped_name = name.replace("'", "\\'")

            duplicate_lookup = await run_query(f"""
            MATCH (existing:PIPELINE {{status:'reusable'}})
            WHERE toLower(trim(coalesce(existing.name, ''))) = toLower(trim('{escaped_name}'))
            RETURN {{pipeline_uid:existing.uid,
                     version_uid:existing.active_version_uid,
                     pipeline_name:existing.name}} AS reusable_pipeline
            LIMIT 1;
            """, "find_reusable_pipeline_by_name")
            duplicate_rows = json.loads(duplicate_lookup) if isinstance(duplicate_lookup, str) else duplicate_lookup
            if isinstance(duplicate_rows, list) and duplicate_rows:
                raise ValueError(
                    "A reusable pipeline with this name already exists. "
                    "Call list_reusable_pipelines and pin its saved version instead."
                )

            def escaped_json(value: Any) -> str:
                return json.dumps(value, ensure_ascii=True, sort_keys=True).replace("\\", "\\\\").replace("'", "\\'")

            escaped_description = description.replace("'", "\\'")
            escaped_version_name = version_name.replace("'", "\\'")
            graph_json = escaped_json(graph)
            interface_json = escaped_json(interface)
            public_ports_json = escaped_json(public_ports)
            query = f"""
            CREATE (p:PIPELINE {{
              uid: randomUUID(), name:'{escaped_name}', label:'{escaped_name}',
              description:'{escaped_description}', status:'reusable',
              created_at:datetime(), updated_at:datetime()
            }})
            CREATE (v:PIPELINE_VERSION {{
              uid:randomUUID(), name:'{escaped_version_name}', version:'{escaped_version_name}',
              version_index:1, is_main:false, description:'{escaped_description}',
              graph_json:'{graph_json}', interface_json:'{interface_json}',
              public_ports_json:'{public_ports_json}',
              node_count:{len(graph.get('nodes') or [])}, edge_count:{len(graph.get('edges') or [])},
              file_count:0, created_at:datetime(), updated_at:datetime()
            }})
            MERGE (p)-[:HAS_VERSION]->(v)
            SET p.active_version_uid = v.uid
            RETURN {{
              pipeline_uid:p.uid, pipeline_name:p.name,
              version_uid:v.uid, version_name:v.name,
              interface_json:v.interface_json,
              public_ports_json:v.public_ports_json
            }} AS reusable_pipeline;
            """
            return repr(await run_query(query, "create_reusable_pipeline"))
        except Exception as exc:
            raise RuntimeError(f"create_reusable_pipeline failed: {exc}") from exc

    async def configure_subpipeline_step(params: str) -> str:
        """Pins an existing parent Subpipeline step to a saved reusable pipeline version.

        params JSON:
        {
          "flow_id": "parent Subpipeline step flow_id",
          "pipeline_uid": "saved reusable pipeline uid",
          "version_uid": "immutable reusable pipeline version uid"
        }

        The public contract is loaded from the saved version. Never pass or embed
        a graph in this call.
        """
        try:
            data = json.loads(params)

            def valid_id(name: str) -> str:
                value = str(data.get(name) or "").strip()
                if not value or not all(character.isalnum() or character in "_.-" for character in value):
                    raise ValueError(f"configure_subpipeline_step requires valid {name}")
                return value

            flow_id = valid_id("flow_id")
            pipeline_uid = valid_id("pipeline_uid")
            version_uid = valid_id("version_uid")
            lookup = await run_query(f"""
            MATCH (rp:PIPELINE {{uid:'{pipeline_uid}', status:'reusable'}})-[:HAS_VERSION]->(rv:PIPELINE_VERSION {{uid:'{version_uid}'}})
            RETURN {{
              pipeline_uid:rp.uid, pipeline_name:rp.name,
              version_uid:rv.uid, version_name:rv.name,
              interface_json:rv.interface_json,
              public_ports_json:rv.public_ports_json
            }} AS reusable_pipeline;
            """, "resolve_reusable_pipeline")
            decoded = json.loads(lookup) if isinstance(lookup, str) else lookup
            rows = decoded if isinstance(decoded, list) else []
            reusable = rows[0].get("reusable_pipeline") if rows and isinstance(rows[0], dict) else None
            if not isinstance(reusable, dict):
                raise ValueError("Reusable pipeline version was not found")
            try:
                interface = json.loads(reusable.get("interface_json") or "{}")
                public_ports = json.loads(reusable.get("public_ports_json") or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError("Reusable pipeline version has an invalid public contract") from exc
            if not isinstance(interface, dict) or not isinstance(public_ports, dict):
                raise ValueError("Reusable pipeline version has no public contract")
            reference = {
                "pipeline_uid": pipeline_uid,
                "pipeline_name": str(reusable.get("pipeline_name") or ""),
                "version_uid": version_uid,
                "version_name": str(reusable.get("version_name") or ""),
            }
            definition = {"version": 2, "reference": reference, "interface": interface}
            serialized_definition = json.dumps(definition, ensure_ascii=True, sort_keys=True)
            serialized_ports = ports_json(public_ports, "subpipeline")
            escaped_definition = serialized_definition.replace("\\", "\\\\").replace("'", "\\'")
            escaped_ports = serialized_ports.replace("\\", "\\\\").replace("'", "\\'")
            input_ids = [str(port.get("id") or "") for port in public_ports.get("inputs", []) if isinstance(port, dict)]
            output_ids = [str(port.get("id") or "") for port in public_ports.get("outputs", []) if isinstance(port, dict)]
            if not input_ids or not output_ids:
                raise ValueError("Reusable pipeline version requires public inputs and outputs")

            parent_lookup = await run_query(f"""
            MATCH (:PIPELINE {{status:'design'}})-[:HAS_STEP]->(step:STEP {{flow_id:'{flow_id}'}})
            WHERE step.type = 'subpipeline'
            OPTIONAL MATCH (:STEP)-[incoming:FLOWS_TO]->(step)
            OPTIONAL MATCH (step)-[outgoing:FLOWS_TO]->(:STEP)
            RETURN {{current_ports_json:step.ports_json,
                     connected_inputs:collect(DISTINCT incoming.target_port),
                     connected_outputs:collect(DISTINCT outgoing.source_port)}} AS subpipeline_context;
            """, "inspect_subpipeline_contract")
            decoded_parent = json.loads(parent_lookup) if isinstance(parent_lookup, str) else parent_lookup
            parent_rows = decoded_parent if isinstance(decoded_parent, list) else []
            parent_context = (
                parent_rows[0].get("subpipeline_context")
                if parent_rows and isinstance(parent_rows[0], dict)
                else None
            )
            if not isinstance(parent_context, dict):
                raise ValueError("No parent Subpipeline step matched the supplied flow_id")
            migration = plan_subpipeline_port_migration(
                normalize_node_ports(parent_context.get("current_ports_json"), "subpipeline"),
                public_ports,
                [str(value or "") for value in parent_context.get("connected_inputs", []) if value],
                [str(value or "") for value in parent_context.get("connected_outputs", []) if value],
            )
            if not migration["compatible"]:
                conflicts = "; ".join(
                    f"{item.get('direction')} port {item.get('port')}: {item.get('reason')}"
                    for item in migration["conflicts"]
                )
                raise ValueError(
                    "The selected reusable version has an ambiguous connection migration. "
                    "Use disconnect_steps to remove the affected edges, configure the Subpipeline, "
                    f"then reconnect explicit compatible ports. {conflicts}"
                )

            def migration_case(variable: str, mapping: dict[str, str]) -> str:
                clauses = []
                for old_port, new_port in mapping.items():
                    escaped_old = old_port.replace("\\", "\\\\").replace("'", "\\'")
                    escaped_new = new_port.replace("\\", "\\\\").replace("'", "\\'")
                    clauses.append(f"WHEN {variable} = '{escaped_old}' THEN '{escaped_new}'")
                if not clauses:
                    return variable
                return f"CASE {' '.join(clauses)} ELSE {variable} END"

            incoming_migration = migration_case("connection.target_port", migration["input_mapping"])
            outgoing_migration = migration_case("connection.source_port", migration["output_mapping"])
            query = f"""
            MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(subpipelineStep:STEP {{flow_id:'{flow_id}'}})
            WHERE subpipelineStep.type = 'subpipeline'
            SET subpipelineStep.subpipeline_json = '{escaped_definition}',
                subpipelineStep.ports_json = '{escaped_ports}',
                subpipelineStep.primary_input_port = '{_cypher_string(input_ids[0])}',
                subpipelineStep.primary_output_port = '{_cypher_string(output_ids[0])}',
                p.updated_at = datetime()
            WITH p, subpipelineStep
            OPTIONAL MATCH (:STEP)-[incoming:FLOWS_TO]->(subpipelineStep)
            WITH p, subpipelineStep, collect(incoming) AS incomingFlows
            OPTIONAL MATCH (subpipelineStep)-[outgoing:FLOWS_TO]->(:STEP)
            WITH p, subpipelineStep, incomingFlows, collect(outgoing) AS outgoingFlows
            FOREACH (connection IN incomingFlows |
              SET connection.target_port = {incoming_migration}
            )
            FOREACH (connection IN outgoingFlows |
              SET connection.source_port = {outgoing_migration}
            )
            RETURN {{flow_id:subpipelineStep.flow_id,
                     referenced_pipeline_uid:'{pipeline_uid}',
                     referenced_version_uid:'{version_uid}',
                     public_inputs:{json.dumps(input_ids)},
                     public_outputs:{json.dumps(output_ids)},
                     input_port_mapping:{json.dumps(migration['input_mapping'])},
                     output_port_mapping:{json.dumps(migration['output_mapping'])},
                     pipeline_updated_at:toString(p.updated_at)}} AS subpipeline_step;
            """
            result = await run_query(query, "configure_subpipeline_step")
            if _agent_query_returned_no_rows(result):
                raise ValueError("No parent Subpipeline step matched the supplied flow_id")
            return repr(result)
        except Exception as exc:
            raise RuntimeError(f"configure_subpipeline_step failed: {exc}") from exc

    async def insert_step(params: str) -> str:
        """Inserts a STEP before an existing STEP.

        params JSON:
        {
          "type": "source|task|destination|flow|subpipeline",
          "label": "step label",
          "description": "step description",
          "template": "optional template name",
          "parameters": {"static_parameter": "value"},
          "secret_parameters": ["api_key"],
          "implementation": "optional model implementation object from create_step",
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
            raw_label = str(data.get("label", ""))
            raw_description = str(data.get("description", ""))
            label = raw_label.replace("'", "\\'")
            description = raw_description.replace("'", "\\'")
            resolved_template = _resolved_template_for_step(
                step_type,
                data.get("template"),
                raw_label,
                raw_description,
            )
            before_flow_id = str(data["before_flow_id"]).replace("'", "\\'")
            raw_after_flow_id = data.get("after_flow_id")
            after_flow_id = (
                str(raw_after_flow_id).replace("'", "\\'")
                if raw_after_flow_id is not None and str(raw_after_flow_id).strip() != ""
                else None
            )
            props_str = ",\n            ".join(
                _step_props_lines(
                    step_type,
                    label,
                    description,
                    resolved_template.replace("'", "\\'"),
                    data.get("implementation"),
                    data.get("parameters"),
                    data.get("secret_parameters"),
                )
            )

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
        except KeyError as exc:
            raise ValueError("insert_step requires before_flow_id") from exc
        except Exception as exc:
            raise RuntimeError(f"insert_step failed: {exc}") from exc

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
            raise RuntimeError(f"delete_step failed: {exc}") from exc

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
            raise RuntimeError(f"delete_all_steps failed: {exc}") from exc

    user_proxy = UserProxyAgent("user_proxy")
    _ = user_proxy

    pipeline_editor = AssistantAgent(
        name="pipeline_editor",
        model_client=model_client,
        tools=[
            create_pipeline,
            create_step,
            configure_flow_step,
            list_reusable_pipelines,
            create_reusable_pipeline,
            configure_subpipeline_step,
            connect_steps,
            disconnect_steps,
            insert_step,
            delete_step,
            delete_all_steps,
            overview,
        ],
        description="An agent that designs AI/data pipelines given a user request.",
        system_message="""You design AI/data pipelines using your registered tools. Use the tools to create or modify the persisted pipeline as requested by the user.
                          A PIPELINE is composed of one or several STEPs. Use overview to check if there are any pipelines. If the user request is unclear or incomplete, ask for more details.
                        - [overview]: calling this tool will give you an overview of the current pipeline content, if any.
                        - [create_pipeline]: calling this tool will create a pipeline.
                        - [create_step]: calling this tool will create a new step in a pipeline (will always place it last). For type subpipeline it also resolves and pins the saved reusable version atomically.
                        - [configure_flow_step]: calling this tool will configure an existing Flow as Condition or Parallel Map and migrate its generic connection handles.
                        - [list_reusable_pipelines]: calling this tool lists separately saved reusable pipelines and their immutable versions.
                        - [create_reusable_pipeline]: calling this tool creates a distinct reusable pipeline and its first immutable version from a complete runnable graph.
                        - [configure_subpipeline_step]: calling this tool pins a parent Subpipeline component to a saved reusable pipeline version; it never accepts an embedded graph.
                        - [connect_steps]: calling this tool will create or configure a connection between two steps using explicit output and input port ids. Use it for branches, merges, and to correct connection handles.
                        - [disconnect_steps]: calling this tool will remove one exact connection identified by its source, target, and port ids. Use it to repair shortcuts, stale branches, and accidental duplicate paths.
                        - [insert_step]: calling this tool will insert a new step before an existing step. Use it instead of create_step when the user asks to add a step between existing steps (for example, "add preprocessing between ingestion and training") or as a new initial step (for example, "add an initial validation step"). For between-step insertion, pass after_flow_id and before_flow_id for directly connected steps. For initial insertion, pass only before_flow_id; the target must currently have no incoming FLOWS_TO edge.
                        - [delete_step]: calling this tool will delete a step in a pipeline.
                        - [delete_all_steps]: calling this tool will remove every step from the current design pipeline while keeping the pipeline itself. Use it only when the user asks to clear, empty, reset, delete all steps, remove everything from, or remove all nodes/steps in the pipeline.
                        Tool calls MUST use a single string argument named params. The value of params MUST be a JSON-encoded string matching the "params JSON" schema in the docstring.
                        Graph writes are ordered operations. Call exactly ONE mutating tool at a time, wait for its result, and stop immediately to correct any failed tool call. Never batch create_pipeline, create_step, configure_flow_step, create_reusable_pipeline, configure_subpipeline_step, connect_steps, disconnect_steps, insert_step, or delete operations in one response.
                        When creating, designing, regenerating, or rebuilding a pipeline, call create_pipeline first with a concise generated name and a fresh 1-2 sentence description that summarizes the full intended pipeline. Do this before creating steps so the UI pipeline description is updated.
                        For a new pipeline, plan the complete dependency order before the first create_step call, then call create_step once per step in actual execution order from ingress to terminal delivery. Because create_step appends and connects to the current tail, calling it in reverse conceptual order creates a wrong graph.
                        A Flow is executable control logic, not a label or decorative box. Never use the legacy generic template "Flow". Use template "Condition" with parameters.expression for routing, or template "Parallel Map" with max_concurrency and failure_policy for fan-out. Condition expressions must compare value or value.field with a literal, for example value.sentiment == "negative". The Condition input handle is value; its output handles are when_true and when_false. Parallel Map uses items and item.
                        Keep the graph minimal: do not add shortcut or bypass edges. Only Flow steps fan out by default. A regular source, task, destination, or subpipeline should have at most one distinct downstream target unless the user explicitly asks for that step's output to feed multiple independent consumers; only then pass allow_fan_out:true to connect_steps.
                        For a conditional request, make the exceptional branch the true branch when practical. Append that branch in execution order, then call connect_steps to add the alternate branch. Canonical condition example: for "if sentiment is negative, create a complaint and update stats; otherwise update stats", build Input -> Condition(expression: value.sentiment == "negative"); Condition.when_true -> Complaint.input -> Update Stats.input; Condition.when_false -> Update Stats.input; Update Stats.output -> Delivery.data. Those are the only edges. Complaint has exactly one outgoing edge, to Update Stats; never connect Complaint directly to Delivery. Both requested outcomes must appear as real FLOWS_TO connections; never merely describe branches in the chat.
                        Canonical parallel-map example: for "resize every uploaded image independently, at most four at a time, continue if one image fails, then export", build Upload -> Parallel Map(template "Parallel Map", parameters max_concurrency:4 and failure_policy:"continue") -> Resize Image -> Export. Connect Upload.data -> Parallel Map.items, Parallel Map.item -> Resize Image.input, and Resize Image.output -> Export.data. The Parallel Map owns iteration; do not duplicate one Resize step per item and do not create condition-style true/false branches for a Parallel Map.
                        When overview shows an existing generic Flow, repair it with configure_flow_step instead of creating a duplicate. Then use connect_steps to make the requested branch topology explicit.
                        The create_step and insert_step type MUST be one of: source, task, destination, flow, subpipeline. This set is structural and must not grow for technologies or business operations. Use source for external ingress adapters; task for processing and templates such as cleaning, OCR, speech-to-text, LLM, SQL, or API calls; destination for external delivery adapters; flow for conditions and parallel maps; and subpipeline for reusable pipelines.
                        A Subpipeline is a version-pinned invocation of another distinct saved PIPELINE, never an embedded graph or decorative group. Use it only when at least two cohesive operations form a reusable capability with a stable contract. First call list_reusable_pipelines. When a suitable version exists, call create_step with type subpipeline plus reusable_pipeline_uid and reusable_version_uid from that exact list result; create_step pins the reference and loads its public ports in the same atomic operation. If none exists, call create_reusable_pipeline with a complete standalone graph, then create the parent Subpipeline using the returned pipeline_uid and version_uid. configure_subpipeline_step exists to repin an already-created legacy component, not as a required second phase for new components. Reusable pipeline names are unique; never retry creation under the same name. Never finish with an unreferenced Subpipeline component.
                        A referenced Subpipeline placed alone on the canvas is an incomplete parent-pipeline draft, not a broken reusable definition. If overview shows that its reference and interface are already configured, do not call configure_subpipeline_step again. A missing-required-input error means the parent graph lacks an upstream connection: insert or create the appropriate Source before the existing Subpipeline and connect it to the exact public input. Then connect the Subpipeline's public output to the requested downstream processing and a terminal Destination. For an otherwise empty parent containing one configured Subpipeline, use insert_step with before_flow_id to add the Source before it; do not append a Source after it or recreate the Subpipeline.
                        A reusable pipeline is independently runnable and follows the same graph rules as the parent: it has Source and Destination boundaries, a connected execution path, and implemented task steps. Every reusable-pipeline node must include explicit ports.inputs and ports.outputs with stable ids, required flags, and meaningful types; create_reusable_pipeline rejects implicit generic contracts. Source outputs define its public inputs; Destination inputs define its public outputs. The parent component caches that contract but owns none of the referenced steps.
                        Canonical reusable-pipeline example: create a distinct "Conversation Understanding" pipeline that receives Audio and returns one structured Object. Its graph is Audio Input(source output audio:Audio) -> Transcription(task audio:Audio to transcript:Text, generated-code trusted_heavy_model) -> PII Redaction(task transcript:Text to redacted_transcript:Text, generated-code deterministic). Feed redacted_transcript both to Sentiment Analysis(text:Text to sentiment:Object, generated-code trusted_heavy_model) and Conversation Summary.transcript; feed Sentiment Analysis.sentiment to Conversation Summary.sentiment. Conversation Summary returns conversation_analysis:Object using generated-code custom_model, then connects to Structured Analysis Output(destination input conversation_analysis:Object). Pin the parent Subpipeline node to the returned reusable pipeline version. A parent-level sentiment condition remains outside that reusable pipeline when it controls complaint and statistics branches.
                        Templates are metadata on a structural kind. Implementations are separate metadata and may be generated code, Python, SQL, a container, a Git repository, REST API, shell, custom, or a future runtime.
                        Always pass the most specific useful template for each step. Do not use generic template names such as Source, Task, or Destination when the requested capability identifies an adapter or operation. For example, remote-device REST ingestion uses source + REST API, preprocessing uses task + Data Cleaning, model training uses task + Model Training, and clinician email/SMS alerts use destination + Notification.
                        A destination is terminal and cannot feed another step. Model an intermediate database, vector index, cache, or storage-and-retrieval adapter as a task with an output whenever downstream work consumes it. For document retrieval, the dependency order is ingestion -> chunking -> embeddings -> vector indexing/storage -> question answering -> answer delivery; vector indexing/storage is a task and answer delivery is the terminal destination.
                        Use implementation kind rest-api only when an exact endpoint is known and include that endpoint in the implementation object. For internal analytical or transformation work with no external endpoint, use generated-code or python. Do not label an LLM, embedding model, or other internal inference task as rest-api merely because it may eventually be served behind an API.
                        Static configuration belongs in the node's parameters object. Never create a configuration node unless configuration is dynamically produced as pipeline data, in which case model it through ordinary ports.
                        Mark credentials such as API keys, access tokens, client secrets, and passwords in secret_parameters. Never invent or place a real credential in the graph.
                        Use overview to find the relevant flow_id values before calling insert_step unless the flow_id values are already provided by the user.
                        Use the label/description fields for domain-specific names such as ingestion, preprocessing, model training, or alerting.
                        Represent every capability explicitly requested by the user in the graph before finishing. Do not stop after an intermediate storage or transformation step when the request also asks for a terminal behavior. For example, a request that says "answers questions" must include a connected destination that publishes the answer. Re-read the user's request after tool calls and add any missing capability as a connected STEP.
                        This is a one-shot, quality-first design. Choose an implementation class for every analytical step: deterministic processing, classical ML training, trusted pretrained inference, or an explicitly requested custom model. That semantic class belongs in execution_profile and related model-plan fields; implementation.kind is only the runtime packaging kind from the create_step schema. For trusted pretrained inference that inLUMEN will generate and package, use kind generated-code with execution_profile trusted_heavy_model. Never put trusted-pretrained-inference, classical-ml-training, or deterministic-processing in implementation.kind.
                        Default structured/tabular model-training tasks to a scikit-learn implementation unless the user explicitly requests deep learning, a pretrained model, or a named framework/model. Do not invent a pretrained model merely because the domain is specialized.
                        Use trusted pretrained inference for tasks that require it, including speech-to-text and transcript sentiment analysis. Put the task and runtime preferences in the create_step or insert_step implementation object; InLUMEN's routing and trusted-adapter registries resolve supported tasks to verified execution profiles, model IDs, revisions, packages, and quality policies before persistence and code generation.
                        Never invent model repository identifiers, revisions, benchmark claims, dataset provenance, or capabilities. Prefer a real classical baseline over an unverified neural model. Do not choose PocketSphinx, VADER, keyword lists, dummy estimators, or similarly limited substitutes for tasks that require a trusted pretrained model.
                        Select domain-specific or multilingual models when the request indicates that need. For long-text analysis, include chunking and aggregation parameters. For speech recognition, include language strategy, decoding parameters, timestamps, voice activity detection, and diarization requirements when relevant.
                        Deterministic source and destination adapters may omit implementation. Deterministic task steps such as chunking, file conversion, intermediate storage, or report assembly still require a generated-code or python implementation; do not invent a learned model for them.
                        Before claiming success, call overview after the last mutation and verify that every requested capability exists in dependency order and is connected. Never describe a partial graph as completed, and never ask the user to discover or repair tool failures in the UI.
                        """,
        max_tool_iterations=30,
        reflect_on_tool_use=True,
    )

    return RoundRobinGroupChat([pipeline_editor], max_turns=1)
