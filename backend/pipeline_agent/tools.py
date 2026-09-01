import json
from typing import Any

from connector_catalog import require_supported_connector
from graph_client import run_neo4j_query
from node_definitions.registry import get_node_definition_registry
from node_ports import (
    default_input_port_id,
    default_output_port_id,
    normalize_node_ports,
    ports_for_template,
    ports_json,
    ports_json_for_template,
)
from pipeline_agent.contract import (
    COMPONENT_DEFINITION_IDS,
    default_input_port_expression,
    default_output_port_expression,
    require_agent_step_type,
    validate_insertion_kind,
)
from pipeline_graph_validation import FLOW_EXPRESSION_PATTERN, validate_pipeline_graph
from subpipeline_reference import (
    derive_subpipeline_interface,
    normalize_reusable_pipeline_graph,
    plan_subpipeline_port_migration,
    public_ports_for_interface,
)


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


def build_pipeline_editor_tools(
    authorization: str | None = None,
    provenance_context: dict | None = None,
) -> list[Any]:
    """Build request-scoped tools that mutate the persisted pipeline graph."""

    async def run_query(query: str, query_type: str) -> str:
        """Run a Cypher query against Neo4j and return results."""
        return await run_neo4j_query(
            query,
            query_type,
            authorization=authorization,
            provenance_context=provenance_context,
        )

    async def overview() -> str:
        """Gives a design-only overview of the pipeline and its connections."""
        try:
            query_type = "overview"
            query = """
            MATCH (p:PIPELINE)
            OPTIONAL MATCH (p)-[:HAS_STEP]->(s:STEP)
            OPTIONAL MATCH (s)-[r:FLOWS_TO]->(t:STEP)
            RETURN
            p {
                .uid, .name, .label, .description, .version, .status,
                created_at: toString(p.created_at),
                updated_at: toString(p.updated_at)
                } AS pipeline,
            s {
                .uid, .flow_id, .type, .label, .description, .template_label,
                .configuration_status, .ports_json, .primary_input_port,
                .primary_output_port, .x, .y
                } AS step,
            CASE
                WHEN s IS NULL OR s.flow_id IS NULL THEN NULL
                WHEN toString(s.flow_id) =~ '^[0-9]+$' THEN toInteger(s.flow_id)
                ELSE NULL
            END AS step_order,
            r { .source_port, .target_port } AS flow,
            t {
                .uid, .flow_id, .type, .label, .description, .template_label,
                .configuration_status, .ports_json, .primary_input_port,
                .primary_output_port, .x, .y
                } AS next_step
            ORDER BY pipeline.label, step_order;
            """
            result = await run_query(query, query_type)
            return repr(result)
        except Exception as exc:
            raise RuntimeError(f"overview failed: {exc}") from exc

    async def list_pipeline_components(params: str) -> str:
        """Lists the enabled structural components available in the UI palette.

        params JSON: {}

        The returned ids and versions are the authoritative component choices.
        Templates configure a component; they never introduce another structural
        type.
        """
        _ = json.loads(params) if params else {}
        definitions = get_node_definition_registry().list()
        return json.dumps(
            {
                "components": [
                    {
                        "definition_id": definition.id,
                        "definition_version": definition.version,
                        "type": definition.base_type,
                        "label": definition.palette.label,
                        "description": definition.palette.description,
                    }
                    for definition in definitions
                ],
                "rule": (
                    "Use only these structural types. Design labels, descriptions, "
                    "control-flow behavior, and ports. Source and Destination use "
                    "their zero-configuration default boundaries; runtime "
                    "implementation and parameters are configured after design."
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

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
    ) -> list[str]:
        """Builds shared STEP properties for create and insert tools."""
        default_template_labels = {
            "source": "Custom",
            "task": "Task",
            "destination": "Custom",
            "flow": "Condition",
            "subpipeline": "Subpipeline",
        }
        resolved_template_label = template_label or default_template_labels[step_type]
        if implementation or parameters or secret_parameters:
            raise ValueError(
                "Pipeline design tools do not accept implementations, runtime "
                "parameters, credentials, or secret names. Configure those after "
                "the high-level pipeline has been designed."
            )
        if step_type == "flow":
            if resolved_template_label not in {"Condition", "Parallel Map"}:
                raise ValueError(
                    "Flow template must be Condition or Parallel Map; generic Flow has no behavior."
                )
        props_lines = [
            f"type:       '{step_type}'",
            f"label:      '{label}'",
            f"description:'{description}'",
            f"template_label:'{resolved_template_label}'",
            f"definition_id:'{COMPONENT_DEFINITION_IDS[step_type]}'",
            "definition_version:1",
            "has_files: 'no'",
            "configuration_status:'unconfigured'",
        ]
        template_json = json.dumps(
            {
                "id": COMPONENT_DEFINITION_IDS[step_type],
                "name": resolved_template_label,
            },
            ensure_ascii=True,
            sort_keys=True,
        ).replace("\\", "\\\\").replace("'", "\\'")
        props_lines.append(f"template_json: '{template_json}'")
        default_ports = ports_json_for_template(
            None,
            step_type,
            resolved_template_label,
        ).replace("\\", "\\\\").replace("'", "\\'")
        props_lines.append(f"ports_json: '{default_ports}'")
        primary_input = default_input_port_id(step_type, resolved_template_label)
        primary_output = default_output_port_id(step_type, resolved_template_label)
        if primary_input:
            props_lines.append(f"primary_input_port: '{primary_input}'")
        if primary_output:
            props_lines.append(f"primary_output_port: '{primary_output}'")
        if step_type == "destination":
            props_lines.append("content: ''")
        props_lines.append("param_json: '{}'")
        props_lines.append("secret_params_json: '[]'")
        return props_lines

    def _resolved_template_for_step(
        step_type: str,
        template_label: object,
        label: object,
        description: object,
    ) -> str:
        raw_template = str(template_label or "").strip()
        if step_type in {"source", "destination"}:
            # Connector choice is an advanced user configuration. Assistant-
            # designed boundaries always use the managed zero-config default.
            return "Custom"
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

    def _available_connection_ports(
        step: dict[str, Any],
        direction: str,
    ) -> list[str]:
        raw_ports = step.get("ports_json")
        ports = ports_for_template(
            raw_ports if raw_ports else None,
            step.get("type"),
            step.get("template_label"),
        )
        values = ports.get(direction)
        return [
            str(port.get("id") or "").strip()
            for port in values or []
            if isinstance(port, dict) and str(port.get("id") or "").strip()
        ]

    def _resolve_connection_port(
        requested: object,
        available: list[str],
        *,
        flow_id: str,
        direction: str,
        primary_port: object = None,
    ) -> str:
        label = "output" if direction == "outputs" else "input"
        value = str(requested or "").strip()
        if not available:
            raise ValueError(f"Step {flow_id} exposes no {label} ports")

        if not value:
            if len(available) == 1:
                return available[0]
            valid = ", ".join(available)
            raise ValueError(
                f"connect_steps requires {label}_port for step {flow_id}. "
                f"Valid {label} port ids: {valid}"
            )

        if not all(character.isalnum() or character in "_.-" for character in value):
            raise ValueError(f"connect_steps received an invalid {label}_port")

        if value in available:
            return value

        # Some models interpret documentation such as "task.output" as the
        # literal id. Port ids are local to a node, so accept that common alias
        # only when its final segment unambiguously names a real port.
        unprefixed = value.rsplit(".", 1)[-1]
        if unprefixed in available:
            return unprefixed

        primary = str(primary_port or "").strip()
        if unprefixed in {"input", "output"} and primary in available:
            return primary

        casefolded = {
            candidate.casefold(): candidate
            for candidate in available
        }
        matched = (
            casefolded.get(value.casefold())
            or casefolded.get(unprefixed.casefold())
        )
        if matched:
            return matched

        valid = ", ".join(available)
        raise ValueError(
            f"Unknown {label} port id {value!r} for step {flow_id}. "
            f"Valid {label} port ids: {valid}. Use the exact id without a component prefix."
        )

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
        """Creates a STEP and connects it after an explicit or unambiguous tail.

        params JSON:
        {
          "type": "source|task|destination|flow|subpipeline",
          "label": "step label",
          "description": "step description",
          "template": "optional high-level semantic for Task or Flow only",
          "after_flow_id": "optional explicit predecessor flow_id",
          "source_port": "required when the explicit predecessor has multiple outputs",
          "allow_fan_out": false,
          "reusable_pipeline_name": "required for Subpipeline unless uid is supplied",
          "reusable_pipeline_uid": "optional saved reusable pipeline uid",
          "reusable_version_uid": "optional immutable version uid",
          "reusable_version_name": "optional version name; active version is the default"
        }
        Do not provide parameters, credentials, secret names, model choices, or
        implementation metadata. Source and Destination always use the default
        managed boundary; connector selection is an advanced user configuration.
        Without after_flow_id, the graph must have exactly one topological tail.
        Use after_flow_id plus source_port to create every explicit branch target,
        including a second terminal Destination from Condition.when_false. A Flow
        may fan out by design; a non-Flow predecessor requires allow_fan_out:true.
        Use task descriptions for domain operations and Flow for execution
        control. Creating a subpipeline
        atomically resolves and pins a saved reusable pipeline version and returns
        its exact public input/output ids; an unreferenced placeholder is never
        created. Never create configuration nodes.
        """
        try:
            query_type = "create_step"
            data = json.loads(params)
            step_type = require_agent_step_type(data.get("type"))
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
            require_supported_connector(step_type, resolved_template)
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
                    line for line in props_lines
                    if not line.startswith((
                        "ports_json:",
                        "primary_input_port:",
                        "primary_output_port:",
                    ))
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
            raw_after_flow_id = data.get("after_flow_id")
            after_flow_id = str(raw_after_flow_id or "").strip()
            allow_fan_out = data.get("allow_fan_out") is True
            resolved_source_port = ""
            if after_flow_id:
                if not all(
                    character.isalnum() or character in "_.-"
                    for character in after_flow_id
                ):
                    raise ValueError("create_step received an invalid after_flow_id")
                predecessor_result = await run_query(f"""
                MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(source:STEP {{flow_id:'{_cypher_string(after_flow_id)}'}})
                RETURN {{
                  flow_id: source.flow_id,
                  type: source.type,
                  template_label: source.template_label,
                  ports_json: source.ports_json,
                  primary_output_port: source.primary_output_port
                }} AS predecessor;
                """, "resolve_step_predecessor")
                predecessors = [
                    row.get("predecessor")
                    for row in _decoded_rows(predecessor_result)
                    if isinstance(row.get("predecessor"), dict)
                ]
                if not predecessors:
                    raise ValueError(
                        "create_step predecessor does not exist; call overview and use "
                        "an exact flow_id"
                    )
                predecessor = predecessors[0]
                resolved_source_port = _resolve_connection_port(
                    data.get("source_port"),
                    _available_connection_ports(predecessor, "outputs"),
                    flow_id=after_flow_id,
                    direction="outputs",
                    primary_port=predecessor.get("primary_output_port"),
                )
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
            if after_flow_id:
                escaped_after_flow_id = _cypher_string(after_flow_id)
                escaped_source_port = _cypher_string(resolved_source_port)
                query = f"""
                MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(prev:STEP {{flow_id:'{escaped_after_flow_id}'}})
                OPTIONAL MATCH (p)-[:HAS_STEP]->(sAll:STEP)
                WHERE sAll.flow_id IS NOT NULL AND toString(sAll.flow_id) =~ '^[0-9]+$'
                WITH p, prev, coalesce(max(toInteger(sAll.flow_id)), 0) + 1 AS nextFlowId
                OPTIONAL MATCH (prev)-[:FLOWS_TO]->(existingTarget:STEP)
                WITH p, prev, nextFlowId, collect(DISTINCT existingTarget) AS existingTargets,
                    coalesce(prev.x, 0.0) AS prevX,
                    coalesce(prev.y, 0.0) AS prevY
                WHERE prev.type <> 'destination'
                  AND (prev.type = 'flow' OR {str(allow_fan_out).lower()} OR size(existingTargets) = 0)
                CREATE (s:STEP {{
                uid: randomUUID(),
                {props_str},
                flow_id: toString(nextFlowId),
                x: prevX + 300.0,
                y: CASE
                    WHEN toLower(coalesce(prev.template_label, '')) = 'condition'
                         AND '{escaped_source_port}' = 'when_true' THEN prevY - 180.0
                    WHEN toLower(coalesce(prev.template_label, '')) = 'condition'
                         AND '{escaped_source_port}' = 'when_false' THEN prevY + 180.0
                    ELSE prevY
                   END
                }})
                MERGE (p)-[:HAS_STEP]->(s)
                MERGE (prev)-[flow:FLOWS_TO]->(s)
                SET flow.source_port = '{escaped_source_port}',
                    flow.target_port = '{target_port}',
                    p.updated_at = datetime()
                RETURN {{
                flow_id: s.flow_id,
                uid: s.uid,
                type: s.type,
                label: s.label,
                description: s.description,
                x: s.x,
                y: s.y,
                after_flow_id: prev.flow_id,
                source_port: flow.source_port,
                target_port: flow.target_port,
                pipeline_updated_at: toString(p.updated_at){subpipeline_return}
                }} AS step;
                """
            else:
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
            OPTIONAL MATCH (p)-[:HAS_STEP]->(sAll:STEP)
            WHERE sAll.flow_id IS NOT NULL AND toString(sAll.flow_id) =~ '^[0-9]+$'
            WITH p, coalesce(max(toInteger(sAll.flow_id)), 0) + 1 AS nextFlowId

            OPTIONAL MATCH (p)-[:HAS_STEP]->(candidateTail:STEP)
            WITH p, nextFlowId, collect(candidateTail) AS candidateTails
            WITH p, nextFlowId,
                [candidate IN candidateTails WHERE NOT (candidate)-[:FLOWS_TO]->()] AS tails
            WITH p, nextFlowId, tails,
                CASE WHEN size(tails) = 1 THEN head(tails) ELSE NULL END AS prev
            WHERE size(tails) <= 1

            WITH p, nextFlowId, prev,
                coalesce(prev.x, 0.0) AS prevX,
                coalesce(prev.y, 0.0) AS prevY
            WHERE prev IS NULL OR ('{step_type}' <> 'source' AND prev.type <> 'destination')
            CREATE (s:STEP {{
            uid: randomUUID(),
            {props_str},
            flow_id: toString(nextFlowId),
            x: CASE WHEN prev IS NULL THEN 0.0 ELSE prevX + 300.0 END,
            y: CASE
                WHEN prev IS NULL THEN 0.0
                WHEN toLower(coalesce(prev.template_label, '')) = 'condition' THEN prevY - 180.0
                ELSE prevY
               END
            }})
            MERGE (p)-[:HAS_STEP]->(s)
            FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
            MERGE (prev)-[flow:FLOWS_TO]->(s)
            SET flow.source_port = CASE
                    WHEN prev.type = 'source' THEN coalesce(prev.primary_output_port, 'data')
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
            if _agent_query_returned_no_rows(result):
                raise ValueError(
                    "Step creation rejected: the graph has no unambiguous appendable "
                    "tail, the predecessor is terminal, or non-Flow fan-out was not "
                    "authorized. Use after_flow_id and an exact source_port to create "
                    "an explicit branch target."
                )
            return repr(result)
        except Exception as exc:
            raise RuntimeError(f"create_step failed: {exc}") from exc

    async def connect_steps(params: str) -> str:
        """Creates or configures a port-aware connection between two existing steps.

        params JSON:
        {
          "source_flow_id": "source step flow_id",
          "target_flow_id": "target step flow_id",
          "source_port": "optional exact output port id",
          "target_port": "optional exact input port id",
          "allow_fan_out": false
        }

        Port ids never include a component-type prefix. Standard exact ids are:
        Source output `data`; Task input `input` and output `output`; Destination
        input `data`; Condition input `value` and outputs `when_true`/`when_false`;
        Parallel Map input `items` and output `item`. When a side exposes exactly
        one port, its port field may be omitted and will be inferred.
        Subpipeline handles are the public port ids returned by create_step.
        For compatibility, type-prefixed aliases such as task.output are
        normalized only when they resolve to a real port, and input/output
        aliases are mapped to the pinned Subpipeline's primary public ports.
        Calling this for an existing source/target pair updates that connection's
        handles. Use it to add every non-linear branch.
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
            allow_fan_out = data.get("allow_fan_out") is True
            if source_flow_id == target_flow_id:
                raise ValueError("connect_steps cannot connect a step to itself")

            context_result = await run_query(f"""
            MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(source:STEP {{flow_id:'{source_flow_id}'}})
            MATCH (p)-[:HAS_STEP]->(target:STEP {{flow_id:'{target_flow_id}'}})
            WHERE source.type <> 'destination' AND target.type <> 'source'
            RETURN {{
              source: {{flow_id:source.flow_id, type:source.type,
                       template_label:source.template_label, ports_json:source.ports_json,
                       primary_output_port:source.primary_output_port}},
              target: {{flow_id:target.flow_id, type:target.type,
                       template_label:target.template_label, ports_json:target.ports_json,
                       primary_input_port:target.primary_input_port}}
            }} AS connection_context;
            """, "resolve_connection_ports")
            contexts = [
                row.get("connection_context")
                for row in _decoded_rows(context_result)
                if isinstance(row.get("connection_context"), dict)
            ]
            if not contexts:
                raise ValueError(
                    "Connection rejected: source or target step does not exist, or the "
                    "requested direction is invalid. Call overview and use its flow_id values."
                )

            context = contexts[0]
            source_context = context.get("source")
            target_context = context.get("target")
            if not isinstance(source_context, dict) or not isinstance(target_context, dict):
                raise ValueError("Connection rejected: step port contracts are unavailable")

            source_port = _resolve_connection_port(
                data.get("source_port"),
                _available_connection_ports(source_context, "outputs"),
                flow_id=source_flow_id,
                direction="outputs",
                primary_port=source_context.get("primary_output_port"),
            )
            target_port = _resolve_connection_port(
                data.get("target_port"),
                _available_connection_ports(target_context, "inputs"),
                flow_id=target_flow_id,
                direction="inputs",
                primary_port=target_context.get("primary_input_port"),
            )
            escaped_source_port = _cypher_string(source_port)
            escaped_target_port = _cypher_string(target_port)

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
            SET flow.source_port = '{escaped_source_port}',
                flow.target_port = '{escaped_target_port}',
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
        """Configures an existing Flow step's high-level behavior and ports.

        params JSON for a condition:
        {
          "flow_id": "existing Flow step flow_id",
          "behavior": "Condition",
          "expression": "value.is_valid == true"
        }

        params JSON for a parallel map:
        {
          "flow_id": "existing Flow step flow_id",
          "behavior": "Parallel Map"
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
            if data.get("parameters"):
                raise ValueError(
                    "Pipeline design does not accept Flow runtime parameters; "
                    "configure them after the high-level design phase."
                )
            expression = str(data.get("expression") or "").strip()
            if behavior == "Condition":
                if not expression:
                    raise ValueError(
                        "Condition configuration requires an expression such as "
                        "value.is_valid == true"
                    )
                if not FLOW_EXPRESSION_PATTERN.fullmatch(expression):
                    raise ValueError(
                        "Condition expressions must compare value or value.field "
                        "with a literal"
                    )
            flow_parameters = {"expression": expression} if expression else {}
            escaped_parameters = _cypher_string(json.dumps(flow_parameters, sort_keys=True))
            serialized_ports = ports_json_for_template(None, "flow", behavior)
            escaped_ports = serialized_ports.replace("\\", "\\\\").replace("'", "\\'")
            input_port = default_input_port_id("flow", behavior)
            default_output_port = "when_true" if behavior == "Condition" else "item"
            query = f"""
            MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(flowStep:STEP {{flow_id:'{flow_id}'}})
            WHERE flowStep.type = 'flow'
            SET flowStep.template_label = '{behavior}',
                flowStep.param_json = '{escaped_parameters}',
                flowStep.configuration_status = CASE
                    WHEN '{behavior}' = 'Condition' THEN 'configured'
                    ELSE 'unconfigured'
                END,
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
          "graph": {"nodes": ["complete React Flow-shaped nodes"], "edges": ["connections"]}
        }

        The graph must be structurally complete on its own, with Source and
        Destination boundaries. It is still a high-level design: do not include
        runtime parameters, credentials, environment names, or implementation
        metadata. Port ids and data contracts are inferred and frozen when the
        version is saved; expert-supplied typed ports remain supported. Source
        and Destination ports become the public contract.
        """
        try:
            data = json.loads(params)
            name = str(data.get("name") or "").strip()
            description = str(data.get("description") or "").strip()
            version_name = str(data.get("version_name") or "Version 1").strip() or "Version 1"
            graph = data.get("graph") if isinstance(data.get("graph"), dict) else {}
            if not name:
                raise ValueError("create_reusable_pipeline requires name")
            for raw_node in graph.get("nodes") or []:
                if not isinstance(raw_node, dict):
                    continue
                node_data = (
                    raw_node.get("data")
                    if isinstance(raw_node.get("data"), dict)
                    else raw_node
                )
                if any(
                    node_data.get(field)
                    for field in (
                        "implementation",
                        "param",
                        "parameters",
                        "secret_params",
                        "secret_parameters",
                    )
                ):
                    raise ValueError(
                        "Reusable pipeline design must not include implementations, "
                        "runtime parameters, credentials, environment names, or secrets."
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
          "template": "optional Task or Flow template name",
          "reusable_pipeline_uid": "required when inserting a Subpipeline",
          "reusable_version_uid": "required when inserting a Subpipeline",
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
            step_type = require_agent_step_type(data.get("type"))
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
            require_supported_connector(step_type, resolved_template)
            before_flow_id = str(data["before_flow_id"]).replace("'", "\\'")
            raw_after_flow_id = data.get("after_flow_id")
            after_flow_id = (
                str(raw_after_flow_id).replace("'", "\\'")
                if raw_after_flow_id is not None and str(raw_after_flow_id).strip() != ""
                else None
            )
            validate_insertion_kind(step_type, initial=after_flow_id is None)
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
                resolved_subpipeline = await _resolve_subpipeline_for_creation(
                    data,
                    raw_label,
                )
                props_lines = [
                    line for line in props_lines
                    if not line.startswith((
                        "ports_json:",
                        "primary_input_port:",
                        "primary_output_port:",
                    ))
                ]
                props_lines.extend([
                    f"ports_json: '{_cypher_string(resolved_subpipeline['ports_json'])}'",
                    f"subpipeline_json: '{_cypher_string(resolved_subpipeline['definition_json'])}'",
                    f"primary_input_port: '{_cypher_string(resolved_subpipeline['input_ids'][0])}'",
                    f"primary_output_port: '{_cypher_string(resolved_subpipeline['output_ids'][0])}'",
                ])
            props_str = ",\n            ".join(props_lines)
            new_input_port = (
                resolved_subpipeline["input_ids"][0]
                if resolved_subpipeline
                else default_input_port_id(step_type, resolved_template)
            )
            new_output_port = (
                resolved_subpipeline["output_ids"][0]
                if resolved_subpipeline
                else default_output_port_id(step_type, resolved_template)
            )
            before_input_expression = default_input_port_expression("before")

            if after_flow_id is None:
                query_type = "insert_initial_step"
                query = f"""
                MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(before:STEP {{flow_id: '{before_flow_id}'}})
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
                MERGE (s)-[newFlow:FLOWS_TO]->(before)
                SET newFlow.source_port = '{_cypher_string(new_output_port)}',
                    newFlow.target_port = {before_input_expression}
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
                MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(before:STEP {{flow_id: '{before_flow_id}'}})
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
                    oldFlow.source_port AS oldSourcePort,
                    oldFlow.target_port AS oldTargetPort,
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
                MERGE (after)-[incomingFlow:FLOWS_TO]->(s)
                SET incomingFlow.source_port = coalesce(
                        oldSourcePort,
                        {default_output_port_expression('after')}
                    ),
                    incomingFlow.target_port = '{_cypher_string(new_input_port)}'
                MERGE (s)-[outgoingFlow:FLOWS_TO]->(before)
                SET outgoingFlow.source_port = '{_cypher_string(new_output_port)}',
                    outgoingFlow.target_port = coalesce(
                        oldTargetPort,
                        {before_input_expression}
                    )
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
            if _agent_query_returned_no_rows(result):
                raise ValueError(
                    "Insertion rejected: the referenced design-pipeline steps were not "
                    "found, were not directly connected, or the initial target already "
                    "has an incoming connection."
                )
            return repr(result)
        except KeyError as exc:
            raise ValueError("insert_step requires before_flow_id") from exc
        except Exception as exc:
            raise RuntimeError(f"insert_step failed: {exc}") from exc

    async def delete_step(params: str) -> str:
        """Deletes one STEP from the design pipeline.

        params JSON: {"step_uid": "step uid returned by overview"}

        A simple one-in/one-out chain is reconnected with its original external
        port handles. Branch points and merges are not bridged automatically;
        use connect_steps explicitly after deleting one of those nodes.
        """
        try:
            query_type = "delete_step"
            data = json.loads(params)
            step_uid = str(data["step_uid"]).strip()
            if not step_uid or not all(
                character.isalnum() or character in "_.-" for character in step_uid
            ):
                raise ValueError("delete_step requires a valid step_uid")

            query = f"""
            MATCH (p:PIPELINE {{status:'design'}})-[:HAS_STEP]->(s:STEP {{uid:'{step_uid}'}})
            OPTIONAL MATCH (p)-[:HAS_STEP]->(prev:STEP)-[incomingFlow:FLOWS_TO]->(s)
            WITH p, s, collect(CASE WHEN prev IS NULL THEN null ELSE {{
                node:prev,
                source_port:incomingFlow.source_port
            }} END) AS incoming
            OPTIONAL MATCH (s)-[outgoingFlow:FLOWS_TO]->(next:STEP)<-[:HAS_STEP]-(p)
            WITH p, s, incoming, collect(CASE WHEN next IS NULL THEN null ELSE {{
                node:next,
                target_port:outgoingFlow.target_port
            }} END) AS outgoing
            WITH p, s, incoming, outgoing,
                s.flow_id AS deletedFlowId,
                s.label AS deletedLabel,
                CASE WHEN size(incoming) = 1 THEN incoming[0].node ELSE null END AS previous,
                CASE WHEN size(outgoing) = 1 THEN outgoing[0].node ELSE null END AS following,
                CASE WHEN size(incoming) = 1 THEN incoming[0].source_port ELSE null END AS previousSourcePort,
                CASE WHEN size(outgoing) = 1 THEN outgoing[0].target_port ELSE null END AS followingTargetPort
            FOREACH (_ IN CASE
                WHEN size(incoming) = 1 AND size(outgoing) = 1 THEN [1]
                ELSE []
            END |
                MERGE (previous)-[bridge:FLOWS_TO]->(following)
                SET bridge.source_port = coalesce(
                        previousSourcePort,
                        {default_output_port_expression('previous')}
                    ),
                    bridge.target_port = coalesce(
                        followingTargetPort,
                        {default_input_port_expression('following')}
                    )
            )
            DETACH DELETE s
            SET p.updated_at = datetime()
            RETURN {{
                step_uid:'{step_uid}',
                flow_id:deletedFlowId,
                label:deletedLabel,
                incoming_count:size(incoming),
                outgoing_count:size(outgoing),
                chain_reconnected:size(incoming) = 1 AND size(outgoing) = 1,
                pipeline_updated_at:toString(p.updated_at)
            }} AS deleted_step;
            """
            result = await run_query(query, query_type)
            if _agent_query_returned_no_rows(result):
                raise ValueError("No design-pipeline step matched the supplied step_uid")
            return repr(result)
        except KeyError as exc:
            raise ValueError("delete_step requires step_uid") from exc
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

    return [
        list_pipeline_components,
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
    ]
