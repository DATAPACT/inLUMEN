"""The design agent's graph store on Neo4j: one atomic Cypher query per operation.

Implements ``inlumen.design.store.GraphStore`` for the inLUMEN backend. Each
operation is a single query, so a graph change is one transaction.
"""
from __future__ import annotations

import json
from typing import Any

from graph_client import fetch_pipeline_graph, run_neo4j_query, sync_backend_to_canvas_graph
from inlumen.design.contract import default_input_port_expression, default_output_port_expression
from inlumen.design.store import (
    FlowConfiguration,
    NewStep,
    Predecessor,
    Rows,
    SubpipelineConfiguration,
)

# How each STEP property is written in a CREATE map, as the queries always had it.
_PROPERTY_FORMATS = {
    "type": "type:       '{}'",
    "label": "label:      '{}'",
    "description": "description:'{}'",
    "template_label": "template_label:'{}'",
    "definition_id": "definition_id:'{}'",
    "definition_version": "definition_version:{}",
    "configuration_status": "configuration_status:'{}'",
}
_UNQUOTED = {"definition_version"}

# Reads that must see the shared reusable catalog, not a preview workspace.
_CATALOG_READS = {
    "list_reusable_pipelines",
    "resolve_reusable_pipeline_for_creation",
    "resolve_reusable_pipeline",
    "find_reusable_pipeline_by_name",
}


def _cypher_string(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _props_str(props: dict[str, Any]) -> str:
    lines = []
    for key, value in props.items():
        template = _PROPERTY_FORMATS.get(key, key + ": '{}'")
        rendered = str(value) if key in _UNQUOTED else _cypher_string(value)
        lines.append(template.format(rendered))
    return ",\n            ".join(lines)


def _cypher_map(mapping: dict[str, str]) -> str:
    """A Cypher map literal of strings (JSON's quoted keys are not valid Cypher)."""
    entries = (
        "`" + str(key).replace("`", "``") + "`: '" + _cypher_string(value) + "'"
        for key, value in mapping.items()
    )
    return "{" + ", ".join(entries) + "}"


def _subpipeline_return(step: NewStep) -> str:
    if not step.subpipeline:
        return ""
    reference = step.subpipeline.reference
    return f""",
            referenced_pipeline_uid: '{_cypher_string(reference['pipeline_uid'])}',
            referenced_pipeline_name: '{_cypher_string(reference['pipeline_name'])}',
            referenced_version_uid: '{_cypher_string(reference['version_uid'])}',
            referenced_version_name: '{_cypher_string(reference['version_name'])}',
            public_inputs: {json.dumps(step.subpipeline.input_ids)},
            public_outputs: {json.dumps(step.subpipeline.output_ids)}"""


class Neo4jGraphStore:
    """The active design pipeline of one workspace, or of an isolated preview."""

    def __init__(
        self,
        *,
        authorization: str | None = None,
        provenance_context: dict | None = None,
        workspace_id: str | None = None,
        preview_mode: bool = False,
        active_version_uid: str = "main",
        active_version_name: str = "Main",
    ):
        self.authorization = authorization
        self.provenance_context = provenance_context
        self.workspace_id = workspace_id
        self.preview_mode = preview_mode
        self.active_version_uid = active_version_uid
        self.active_version_name = active_version_name

    async def _run(self, query: str, query_type: str) -> Rows:
        """Run a Cypher query against Neo4j and return results."""
        if self.preview_mode and query_type == "create_reusable_pipeline":
            raise RuntimeError(
                "Creating a reusable pipeline changes the shared reusable catalog and "
                "cannot be included in a graph preview. Ask the user to turn off "
                "Preview graph changes before creating this reusable pipeline."
            )
        workspace_id_for_query = self.workspace_id
        if self.preview_mode and query_type in _CATALOG_READS:
            # Preview workspaces contain only the active design graph. Read the
            # shared immutable catalog from the authenticated workspace so an
            # existing Subpipeline can still be designed and validated.
            workspace_id_for_query = None
        return await run_neo4j_query(
            query,
            query_type,
            authorization=self.authorization,
            provenance_context=self.provenance_context,
            workspace_id=workspace_id_for_query,
        )

    # Reads

    async def get_graph(self) -> dict:
        kwargs = {"workspace_id": self.workspace_id} if self.workspace_id else {}
        return await fetch_pipeline_graph(authorization=self.authorization, **kwargs)

    async def overview(self) -> Rows:
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
        return await self._run(query, "overview")

    async def find_predecessor(self, flow_id: str) -> Rows:
        return await self._run(f"""
                OPTIONAL MATCH (candidate:PIPELINE {{status:'design'}})
                OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
                WITH candidate, count(candidateStep) AS step_count
                ORDER BY step_count DESC, candidate.updated_at DESC
                WITH collect(candidate)[0] AS p
                MATCH (p:PIPELINE)-[:HAS_STEP]->(source:STEP {{flow_id:'{_cypher_string(flow_id)}'}})
                RETURN {{
                  pipeline_uid: p.uid,
                  flow_id: source.flow_id,
                  type: source.type,
                  template_label: source.template_label,
                  ports_json: source.ports_json,
                  primary_output_port: source.primary_output_port
                }} AS predecessor;
                """, "resolve_step_predecessor")

    async def connection_context(self, source_flow_id: str, target_flow_id: str) -> Rows:
        return await self._run(f"""
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

    async def list_reusable_pipelines(self) -> Rows:
        query = """
        MATCH (p:PIPELINE {status:'reusable'})
        OPTIONAL MATCH (p)-[:HAS_VERSION]->(v:PIPELINE_VERSION)
        WITH p, v ORDER BY CASE WHEN v.uid = p.active_version_uid THEN 0 ELSE 1 END, v.created_at DESC
        WITH p, head(collect(v)) AS v
        RETURN {
          pipeline_uid: p.uid, pipeline_name: p.name, description: p.description,
          interface_json: coalesce(p.interface_json, v.interface_json),
          node_count: coalesce(p.node_count, v.node_count),
          edge_count: coalesce(p.edge_count, v.edge_count)
        } AS reusable_pipeline
        ORDER BY p.name;
        """
        return await self._run(query, "list_reusable_pipelines")

    async def find_reusable_pipeline_by_name(self, name: str) -> Rows:
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        return await self._run(f"""
            MATCH (existing:PIPELINE {{status:'reusable'}})
            WHERE toLower(trim(coalesce(existing.name, ''))) = toLower(trim('{escaped_name}'))
            RETURN {{pipeline_uid:existing.uid,
                     pipeline_name:existing.name}} AS reusable_pipeline
            LIMIT 1;
            """, "find_reusable_pipeline_by_name")

    async def find_reusable_pipelines(
        self,
        *,
        pipeline_uid: str,
        pipeline_name: str,
        version_uid: str,
        version_name: str,
    ) -> Rows:
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
            version_filter = "true"

        return await self._run(f"""
        MATCH (rp:PIPELINE {{status:'reusable'}})
        WHERE {pipeline_filter}
        OPTIONAL MATCH (rp)-[:HAS_VERSION]->(rv:PIPELINE_VERSION)
        WHERE {version_filter}
        WITH rp, rv ORDER BY CASE WHEN rv.uid = rp.active_version_uid THEN 0 ELSE 1 END, rv.created_at DESC
        WITH rp, head(collect(rv)) AS rv
        WHERE rp.graph_json IS NOT NULL OR rv IS NOT NULL
        RETURN {{
          pipeline_uid:rp.uid, pipeline_name:rp.name,
          version_uid:rv.uid, version_name:rv.name,
          graph_json:coalesce(rp.graph_json, rv.graph_json),
          interface_json:coalesce(rp.interface_json, rv.interface_json),
          public_ports_json:coalesce(rp.public_ports_json, rv.public_ports_json)
        }} AS reusable_pipeline
        ORDER BY CASE WHEN toLower(trim(rp.name)) = toLower(trim('{escaped_pipeline_name}')) THEN 0 ELSE 1 END,
                 rp.name
        LIMIT 2;
        """, "resolve_reusable_pipeline_for_creation")

    async def get_reusable_pipeline(self, pipeline_uid: str, version_uid: str) -> Rows:
        return await self._run(f"""
            MATCH (rp:PIPELINE {{uid:'{pipeline_uid}', status:'reusable'}})
            OPTIONAL MATCH (rp)-[:HAS_VERSION]->(rv:PIPELINE_VERSION)
            WHERE '{version_uid}' = '' OR rv.uid = '{version_uid}'
            WITH rp, rv ORDER BY CASE WHEN rv.uid = rp.active_version_uid THEN 0 ELSE 1 END, rv.created_at DESC
            WITH rp, head(collect(rv)) AS rv
            WHERE rp.graph_json IS NOT NULL OR rv IS NOT NULL
            RETURN {{
              pipeline_uid:rp.uid, pipeline_name:rp.name,
              version_uid:rv.uid, version_name:rv.name,
              graph_json:coalesce(rp.graph_json, rv.graph_json),
              interface_json:coalesce(rp.interface_json, rv.interface_json),
              public_ports_json:coalesce(rp.public_ports_json, rv.public_ports_json)
            }} AS reusable_pipeline;
            """, "resolve_reusable_pipeline")

    async def inspect_subpipeline_step(self, flow_id: str) -> Rows:
        return await self._run(f"""
            MATCH (:PIPELINE {{status:'design'}})-[:HAS_STEP]->(step:STEP {{flow_id:'{flow_id}'}})
            WHERE step.type = 'subpipeline'
            OPTIONAL MATCH (:STEP)-[incoming:FLOWS_TO]->(step)
            OPTIONAL MATCH (step)-[outgoing:FLOWS_TO]->(:STEP)
            WITH step.ports_json AS current_ports_json,
                 collect(DISTINCT incoming.target_port) AS connected_inputs,
                 collect(DISTINCT outgoing.source_port) AS connected_outputs
            RETURN {{current_ports_json:current_ports_json,
                     connected_inputs:connected_inputs,
                     connected_outputs:connected_outputs}} AS subpipeline_context;
            """, "inspect_subpipeline_contract")

    # Writes

    async def upsert_pipeline(self, name: str, description: str, version: str) -> Rows:
        name = name.replace("\\", "\\\\").replace("'", "\\'")
        description = description.replace("\\", "\\\\").replace("'", "\\'")
        version = version.replace("\\", "\\\\").replace("'", "\\'")
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
        return await self._run(query, "create_pipeline")

    async def create_step(
        self,
        step: NewStep,
        *,
        target_port: str,
        predecessor: Predecessor | None = None,
    ) -> Rows:
        props_str = _props_str(step.props)
        subpipeline_return = _subpipeline_return(step)
        target_port = target_port.replace("\\", "\\\\").replace("'", "\\'")
        if predecessor is not None:
            escaped_after_flow_id = _cypher_string(predecessor.flow_id)
            escaped_pipeline_uid = _cypher_string(predecessor.pipeline_uid)
            escaped_source_port = _cypher_string(predecessor.source_port)
            allow_fan_out = predecessor.allow_fan_out
            query = f"""
                MATCH (p:PIPELINE {{uid:'{escaped_pipeline_uid}', status:'design'}})-[:HAS_STEP]->(prev:STEP {{flow_id:'{escaped_after_flow_id}'}})
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
        elif step.step_type == "source":
            # Every Source is an independent root: parallel inputs are
            # stacked vertically and never chained to an existing tail.
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
            OPTIONAL MATCH (p)-[:HAS_STEP]->(existingSource:STEP {{type:'source'}})
            WITH p, nextFlowId, count(existingSource) AS sourceCount
            CREATE (s:STEP {{
            uid: randomUUID(),
            {props_str},
            flow_id: toString(nextFlowId),
            x: 0.0,
            y: toFloat(sourceCount) * 220.0
            }})
            MERGE (p)-[:HAS_STEP]->(s)
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
        else:
            step_type = step.step_type
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
                [candidate IN candidateTails WHERE NOT (candidate)-[:FLOWS_TO]->(:STEP)] AS tails
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
                    WHEN prev.type = 'flow' AND toLower(coalesce(prev.template_label, '')) = 'collect' THEN 'items'
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
        return await self._run(query, "create_step")

    async def connect_steps(
        self,
        source_flow_id: str,
        target_flow_id: str,
        source_port: str,
        target_port: str,
        allow_fan_out: bool,
    ) -> Rows:
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
        return await self._run(query, "connect_steps")

    async def disconnect_steps(
        self,
        source_flow_id: str,
        target_flow_id: str,
        source_port: str,
        target_port: str,
    ) -> Rows:
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
        return await self._run(query, "disconnect_steps")

    async def configure_flow_step(self, configuration: FlowConfiguration) -> Rows:
        flow_id = configuration.flow_id
        behavior = configuration.behavior
        escaped_parameters = _cypher_string(json.dumps(configuration.parameters, sort_keys=True))
        escaped_ports = configuration.ports_json.replace("\\", "\\\\").replace("'", "\\'")
        input_port = configuration.input_port
        default_output_port = configuration.default_output_port
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
        return await self._run(query, "configure_flow_step")

    async def create_reusable_pipeline(
        self,
        name: str,
        description: str,
        graph: dict,
        interface: dict,
        public_ports: dict,
    ) -> Rows:
        def escaped_json(value: Any) -> str:
            return json.dumps(value, ensure_ascii=True, sort_keys=True).replace("\\", "\\\\").replace("'", "\\'")

        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        escaped_description = description.replace("\\", "\\\\").replace("'", "\\'")
        graph_json = escaped_json(graph)
        interface_json = escaped_json(interface)
        public_ports_json = escaped_json(public_ports)
        query = f"""
            CREATE (p:PIPELINE {{
              uid: randomUUID(), name:'{escaped_name}', label:'{escaped_name}',
              description:'{escaped_description}', status:'reusable',
              graph_json:'{graph_json}', interface_json:'{interface_json}',
              public_ports_json:'{public_ports_json}',
              node_count:{len(graph.get('nodes') or [])}, edge_count:{len(graph.get('edges') or [])},
              created_at:datetime(), updated_at:datetime()
            }})
            RETURN {{
              pipeline_uid:p.uid, pipeline_name:p.name,
              interface_json:p.interface_json,
              public_ports_json:p.public_ports_json
            }} AS reusable_pipeline;
            """
        return await self._run(query, "create_reusable_pipeline")

    async def configure_subpipeline_step(self, configuration: SubpipelineConfiguration) -> Rows:
        flow_id = configuration.flow_id
        pipeline_uid = configuration.pipeline_uid
        version_uid = configuration.version_uid
        input_ids = configuration.input_ids
        output_ids = configuration.output_ids
        escaped_definition = configuration.definition_json.replace("\\", "\\\\").replace("'", "\\'")
        escaped_ports = configuration.ports_json.replace("\\", "\\\\").replace("'", "\\'")

        def migration_case(variable: str, mapping: dict[str, str]) -> str:
            clauses = []
            for old_port, new_port in mapping.items():
                escaped_old = old_port.replace("\\", "\\\\").replace("'", "\\'")
                escaped_new = new_port.replace("\\", "\\\\").replace("'", "\\'")
                clauses.append(f"WHEN {variable} = '{escaped_old}' THEN '{escaped_new}'")
            if not clauses:
                return variable
            return f"CASE {' '.join(clauses)} ELSE {variable} END"

        incoming_migration = migration_case("connection.target_port", configuration.input_mapping)
        outgoing_migration = migration_case("connection.source_port", configuration.output_mapping)
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
                     input_port_mapping:{_cypher_map(configuration.input_mapping)},
                     output_port_mapping:{_cypher_map(configuration.output_mapping)},
                     pipeline_updated_at:toString(p.updated_at)}} AS subpipeline_step;
            """
        return await self._run(query, "configure_subpipeline_step")

    async def insert_step(
        self,
        step: NewStep,
        *,
        before_flow_id: str,
        after_flow_id: str | None,
        new_input_port: str,
        new_output_port: str,
    ) -> Rows:
        props_str = _props_str(step.props)
        before_flow_id = before_flow_id.replace("\\", "\\\\").replace("'", "\\'")
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
            after_flow_id = after_flow_id.replace("\\", "\\\\").replace("'", "\\'")
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
        return await self._run(query, query_type)

    async def delete_step(self, step_uid: str) -> Rows:
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
                CASE WHEN size(incoming) = 1 THEN incoming[0].node ELSE null END AS prev,
                CASE WHEN size(outgoing) = 1 THEN outgoing[0].node ELSE null END AS next,
                CASE WHEN size(incoming) = 1 THEN incoming[0].source_port ELSE null END AS previousSourcePort,
                CASE WHEN size(outgoing) = 1 THEN outgoing[0].target_port ELSE null END AS followingTargetPort
            OPTIONAL MATCH (next)-[:FLOWS_TO*0..]->(downstream:STEP)
            WITH p, s, incoming, outgoing, deletedFlowId, deletedLabel,
                prev, next, previousSourcePort, followingTargetPort,
                collect(DISTINCT downstream) AS downstreamSteps
            FOREACH (node IN CASE
                WHEN size(incoming) <= 1 AND size(outgoing) = 1 THEN downstreamSteps
                ELSE []
            END |
                SET node.x = coalesce(node.x, 0.0) - 300.0
            )
            FOREACH (_ IN CASE
                WHEN size(incoming) = 1 AND size(outgoing) = 1 THEN [1]
                ELSE []
            END |
                MERGE (prev)-[bridge:FLOWS_TO]->(next)
                SET bridge.source_port = coalesce(
                        previousSourcePort,
                        {default_output_port_expression('prev')}
                    ),
                    bridge.target_port = coalesce(
                        followingTargetPort,
                        {default_input_port_expression('next')}
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
        return await self._run(query, "delete_step")

    async def delete_all_steps(self) -> Rows:
        query = """
            OPTIONAL MATCH (candidate:PIPELINE {status:'design'})
            OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
            WITH candidate, count(candidateStep) AS step_count
            ORDER BY step_count DESC, candidate.updated_at DESC
            WITH collect(candidate)[0] AS p
            WHERE p IS NOT NULL
            OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(s:STEP)
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
        return await self._run(query, "delete_all_steps")

    async def restore_graph(self, graph: dict) -> None:
        kwargs = {"workspace_id": self.workspace_id} if self.workspace_id else {}
        await sync_backend_to_canvas_graph(
            graph,
            "main" if self.workspace_id else self.active_version_uid,
            "Main" if self.workspace_id else self.active_version_name,
            authorization=self.authorization,
            **kwargs,
        )
