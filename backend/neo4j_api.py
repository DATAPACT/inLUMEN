from flask import Flask, request, jsonify
from neo4j import GraphDatabase
from auth_middleware import require_auth
import uuid
import json
import os
import tempfile
from typing import Any
from urllib.parse import quote
from runtime_config import add_cors_headers, get_neo4j_settings
from step_types import normalize_step_type
from node_ports import (
    default_input_port_id,
    default_output_port_id,
    normalize_node_ports,
    ports_json,
)
from node_parameters import normalize_secret_param_keys, secret_params_json
from minio_access import create_bucket, list_objects, read_object_bytes, remove_object, upload_object
from model_plans import resolve_implementation_plan
from node_definitions.instance import (
    definition_data_from_properties,
    definition_properties_from_data,
    normalize_definition_properties,
)
from pipeline_graph_validation import validate_pipeline_graph
from subpipeline_reference import (
    derive_subpipeline_interface,
    missing_explicit_port_contracts,
    normalize_reusable_pipeline_graph,
    plan_subpipeline_port_migration,
    public_ports_for_interface,
)

NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD = get_neo4j_settings()

# Global graph data
graph_data = []

app = Flask(__name__)

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

MAIN_VERSION_UID = "main"
MAIN_VERSION_NAME = "Main"
VERSION_FILE_SNAPSHOT_BUCKET = "pipeline-version-file-snapshots"


@app.route('/health', methods=['GET'])
def health():
    try:
        driver.verify_connectivity()
    except Exception as exc:
        return jsonify({'status': 'unavailable', 'details': str(exc)}), 503
    return jsonify({'status': 'ok'}), 200


def _label_exists(session, label_name: str) -> bool:
    result = session.run("CALL db.labels() YIELD label RETURN collect(label) AS labels").single()
    labels = result["labels"] if result and result["labels"] else []
    return label_name in labels


def _default_pipeline_version_name(session) -> str:
    if not _label_exists(session, "PIPELINE_VERSION"):
        return "Version 1"
    record = session.run("""
    MATCH (v:PIPELINE_VERSION)
    WHERE coalesce(v.is_main, false) = false
    RETURN count(v) AS version_count
    """).single()
    version_count = record["version_count"] if record else 0
    return f"Version {int(version_count) + 1}"


def _ensure_design_pipeline(session) -> str:
    record = session.run("""
    OPTIONAL MATCH (candidate:PIPELINE {status:'design'})
    OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
    WITH candidate, count(candidateStep) AS step_count
    ORDER BY step_count DESC, candidate.updated_at DESC
    WITH collect(candidate)[0] AS candidate
    CALL {
      WITH candidate
      WITH candidate WHERE candidate IS NULL
      CREATE (p:PIPELINE {
          uid: randomUUID(),
          name: '',
          label: '',
          description: '',
          version: 'Main',
          active_version_uid: 'main',
          created_at: datetime(),
          updated_at: datetime(),
          status: 'design'
      })
      RETURN p

      UNION

      WITH candidate
      WITH candidate WHERE candidate IS NOT NULL
      SET candidate.version = CASE
            WHEN candidate.active_version_uid IS NULL THEN 'Main'
            ELSE coalesce(candidate.version, 'Main')
          END,
          candidate.active_version_uid = coalesce(candidate.active_version_uid, 'main'),
          candidate.updated_at = coalesce(candidate.updated_at, datetime())
      RETURN candidate AS p
    }
    RETURN p.uid AS pipeline_uid
    """).single()
    return record["pipeline_uid"]


def _file_refs_from_graph_node(flow_id: str, data: dict) -> list[dict]:
    raw_files = data.get("file_buckets") if isinstance(data.get("file_buckets"), list) else data.get("files")
    if not isinstance(raw_files, list):
        return []

    refs = []
    seen = set()
    default_bucket = f"files-step-id-{flow_id}".lower()
    for item in raw_files:
        filename = ""
        bucket = default_bucket
        snapshot_bucket = ""
        snapshot_object = ""
        role = ""
        if isinstance(item, str):
            filename = item.strip()
        elif isinstance(item, dict):
            filename = str(item.get("filename") or item.get("name") or "").strip()
            bucket = str(item.get("bucket") or default_bucket).strip().lower()
            snapshot_bucket = str(item.get("snapshot_bucket") or "").strip().lower()
            snapshot_object = str(item.get("snapshot_object") or "").strip()
            candidate_role = str(item.get("role") or "").strip().lower()
            role = candidate_role if candidate_role in {"code", "data"} else ""
        if not filename:
            continue
        key = (filename, bucket)
        if key in seen:
            continue
        seen.add(key)
        file_ref = {"filename": filename, "bucket": bucket}
        if role:
            file_ref["role"] = role
        if snapshot_bucket and snapshot_object:
            file_ref["snapshot_bucket"] = snapshot_bucket
            file_ref["snapshot_object"] = snapshot_object
        refs.append(file_ref)
    return refs


def _parse_visible_graph(graph: dict) -> tuple[list[dict], list[dict]]:
    raw_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    raw_edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []

    nodes = []
    seen_node_ids = set()
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            continue
        data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else raw_node
        flow_id = str(raw_node.get("id") or data.get("id") or "").strip()
        if not flow_id or flow_id in seen_node_ids:
            continue
        seen_node_ids.add(flow_id)

        position = raw_node.get("position") if isinstance(raw_node.get("position"), dict) else {}
        try:
            x = float(position.get("x", data.get("x", 0)) or 0)
        except Exception:
            x = 0.0
        try:
            y = float(position.get("y", data.get("y", 0)) or 0)
        except Exception:
            y = 0.0

        step_type = normalize_step_type(data.get("type"), default="task")
        files = _file_refs_from_graph_node(flow_id, data)
        props = {
            "flow_id": flow_id,
            "type": step_type,
            "label": str(data.get("label") or ""),
            "description": str(data.get("description") or ""),
            "template_label": str(data.get("template_label") or ""),
            "ports_json": ports_json(data.get("ports"), step_type),
            "x": x,
            "y": y,
        }
        props.update(definition_properties_from_data(data))
        param_obj = (
            dict(data["param"])
            if isinstance(data.get("param"), dict)
            else {}
        )
        if (
            "model_plan" not in param_obj
            and isinstance(data.get("implementation"), dict)
            and data["implementation"]
        ):
            param_obj["model_plan"] = resolve_implementation_plan(
                data["implementation"],
                label=str(data.get("label") or ""),
                description=str(data.get("description") or ""),
            )
        elif isinstance(param_obj.get("model_plan"), dict):
            param_obj["model_plan"] = resolve_implementation_plan(
                param_obj["model_plan"],
                label=str(data.get("label") or ""),
                description=str(data.get("description") or ""),
            )
        # Parameters are inspector metadata for every structural node kind.
        props["param_json"] = json.dumps(
            param_obj,
            ensure_ascii=False,
            sort_keys=True,
        )
        props["secret_params_json"] = secret_params_json(
            data.get("secret_params"),
            param_obj,
        )
        if step_type == "destination":
            props["content"] = str(data.get("content") or "")
        props["has_files"] = "yes" if files else str(data.get("has_files") or "no").lower().strip()
        # Preserve legacy adapter fields during migration without treating them
        # as structural kinds.
        if "endpoint" in data:
            props["endpoint"] = str(data.get("endpoint") or "")
        if "database" in data:
            props["database"] = str(data.get("database") or "").lower().strip()
        nodes.append({"props": props, "files": files})

    visible_ids = {node["props"]["flow_id"] for node in nodes}
    edges = []
    seen_edges = set()
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        source = str(raw_edge.get("source") or "").strip()
        target = str(raw_edge.get("target") or "").strip()
        source_port = str(raw_edge.get("sourceHandle") or raw_edge.get("source_port") or "").strip()
        target_port = str(raw_edge.get("targetHandle") or raw_edge.get("target_port") or "").strip()
        edge_key = (source, target, source_port, target_port)
        if (
            not source
            or not target
            or source == target
            or source not in visible_ids
            or target not in visible_ids
            or edge_key in seen_edges
        ):
            continue
        seen_edges.add(edge_key)
        edges.append({
            "source": source,
            "target": target,
            "source_port": source_port,
            "target_port": target_port,
        })
    return nodes, edges


def _clear_active_steps(session) -> list[str]:
    record = session.run("""
    MATCH (s:STEP)
    WHERE s.flow_id IS NOT NULL
    RETURN collect(toString(s.flow_id)) AS flow_ids
    """).single()
    flow_ids = record["flow_ids"] if record and record["flow_ids"] else []
    session.run("""
    MATCH (s:STEP)
    WHERE s.flow_id IS NOT NULL
    OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
    WITH collect(DISTINCT s) AS steps, collect(DISTINCT f) AS files
    CALL {
      WITH files
      UNWIND files AS file
      WITH file WHERE file IS NOT NULL
      DETACH DELETE file
    }
    WITH steps
    UNWIND steps AS step
    DETACH DELETE step
    """)
    return flow_ids


def _sync_graph_to_session(
    session,
    graph: dict,
    version_name: str | None = None,
    active_version_uid: str | None = None,
    touch_pipeline_updated_at: bool = True,
) -> dict:
    nodes, edges = _parse_visible_graph(graph)
    pipeline_uid = _ensure_design_pipeline(session)
    settings = graph.get("settings") if isinstance(graph.get("settings"), dict) else {}
    settings_json = json.dumps(settings, ensure_ascii=False)

    if not nodes:
        deleted_ids = _clear_active_steps(session)
        record = session.run("""
        MATCH (p:PIPELINE {uid: $pipeline_uid})
        OPTIONAL MATCH (p)-[:HAS_VERSION]->(activeVersion:PIPELINE_VERSION {uid: $active_version_uid})
        SET p.updated_at = CASE
            WHEN $touch_pipeline_updated_at THEN datetime()
            ELSE p.updated_at
          END
        SET p.version = CASE WHEN $version_name IS NULL THEN p.version ELSE $version_name END
        SET p.active_version_uid = CASE
            WHEN $active_version_uid IS NULL THEN p.active_version_uid
            ELSE $active_version_uid
          END
        SET p.settings_json = $settings_json
        SET p.description = CASE
            WHEN $active_version_uid IS NULL THEN p.description
            ELSE coalesce(activeVersion.description, p.description, '')
          END
        RETURN toString(p.updated_at) AS updated_at
        """, pipeline_uid=pipeline_uid,
             version_name=version_name,
             active_version_uid=active_version_uid,
             touch_pipeline_updated_at=touch_pipeline_updated_at,
             settings_json=settings_json).single()
        return {
            "ok": True,
            "pipeline_uid": pipeline_uid,
            "updated_at": record["updated_at"] if record else None,
            "node_count": 0,
            "edge_count": 0,
            "deleted_step_flow_ids": deleted_ids,
        }

    visible_ids = {node["props"]["flow_id"] for node in nodes}
    session.run("""
    MATCH (s:STEP)
    WHERE s.flow_id IS NOT NULL AND NOT toString(s.flow_id) IN $visible_ids
    OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
    WITH collect(DISTINCT s) AS steps, collect(DISTINCT f) AS files
    CALL {
      WITH files
      UNWIND files AS file
      WITH file WHERE file IS NOT NULL
      DETACH DELETE file
    }
    WITH steps
    UNWIND steps AS step
    DETACH DELETE step
    """, visible_ids=list(visible_ids))

    for node in nodes:
        props = node["props"]
        files = node["files"]
        session.run("""
        MATCH (p:PIPELINE {uid: $pipeline_uid})
        MERGE (s:STEP {flow_id: $flow_id})
        ON CREATE SET s.uid = randomUUID()
        SET s += $props
        REMOVE s.source_config, s.source_config_json
        MERGE (p)-[:HAS_STEP]->(s)
        SET p.updated_at = CASE
            WHEN $touch_pipeline_updated_at THEN datetime()
            ELSE p.updated_at
          END
        RETURN s.flow_id AS flow_id
        """, pipeline_uid=pipeline_uid,
             flow_id=props["flow_id"],
             props=props,
             touch_pipeline_updated_at=touch_pipeline_updated_at)

        session.run("""
        MATCH (s:STEP {flow_id: $flow_id})
        OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
        WITH collect(DISTINCT f) AS files
        CALL {
          WITH files
          UNWIND files AS file
          WITH file WHERE file IS NOT NULL
          DETACH DELETE file
        }
        """, flow_id=props["flow_id"])
        for file_ref in files:
            session.run("""
            MATCH (s:STEP {flow_id: $flow_id})
            MERGE (f:FILE {filename: $filename, bucket: $bucket})
            ON CREATE SET f.uid = randomUUID(), f.added_at = datetime()
            SET f.role = CASE WHEN $role = '' THEN f.role ELSE $role END
            MERGE (s)-[:HAS_FILE]->(f)
            """, flow_id=props["flow_id"], filename=file_ref["filename"], bucket=file_ref["bucket"], role=file_ref.get("role", ""))

    session.run("""
    MATCH (:STEP)-[r:FLOWS_TO]->(:STEP)
    DELETE r
    """)

    for edge in edges:
        session.run("""
        MATCH (source:STEP {flow_id: $source})
        MATCH (target:STEP {flow_id: $target})
        MERGE (source)-[r:FLOWS_TO {
          source_port: $source_port,
          target_port: $target_port
        }]->(target)
        """, source=edge["source"], target=edge["target"],
             source_port=edge["source_port"], target_port=edge["target_port"])

    record = session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})
    OPTIONAL MATCH (p)-[:HAS_VERSION]->(activeVersion:PIPELINE_VERSION {uid: $active_version_uid})
    SET p.updated_at = CASE
        WHEN $touch_pipeline_updated_at THEN datetime()
        ELSE p.updated_at
      END
    SET p.version = CASE WHEN $version_name IS NULL THEN p.version ELSE $version_name END
    SET p.active_version_uid = CASE
        WHEN $active_version_uid IS NULL THEN p.active_version_uid
        ELSE $active_version_uid
      END
    SET p.settings_json = $settings_json
    SET p.description = CASE
        WHEN $active_version_uid IS NULL THEN p.description
        ELSE coalesce(activeVersion.description, p.description, '')
      END
    RETURN toString(p.updated_at) AS updated_at
    """, pipeline_uid=pipeline_uid,
         version_name=version_name,
         active_version_uid=active_version_uid,
         touch_pipeline_updated_at=touch_pipeline_updated_at,
         settings_json=settings_json).single()

    return {
        "ok": True,
        "pipeline_uid": pipeline_uid,
        "updated_at": record["updated_at"] if record else None,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "deleted_step_flow_ids": [],
    }


def _graph_with_metadata(graph: dict, updated_at: str | None = None) -> dict:
    graph_with_metadata = dict(graph) if isinstance(graph, dict) else {}
    if updated_at is not None:
        graph_with_metadata["updated_at"] = updated_at
    return graph_with_metadata


def _sync_version_graph_json_updated_at(
    session,
    version_uid: str,
    updated_at: str | None,
    graph: dict | None = None,
) -> None:
    if not updated_at:
        return

    if graph is None:
        record = session.run("""
        MATCH (v:PIPELINE_VERSION {uid: $version_uid})
        RETURN v.graph_json AS graph_json
        """, version_uid=version_uid).single()
        try:
            graph = json.loads((record["graph_json"] if record else None) or "{}")
        except Exception:
            graph = {}

    graph_with_metadata = _graph_with_metadata(graph if isinstance(graph, dict) else {}, updated_at)
    session.run("""
    MATCH (v:PIPELINE_VERSION {uid: $version_uid})
    SET v.graph_json = $graph_json
    """, version_uid=version_uid, graph_json=json.dumps(graph_with_metadata, ensure_ascii=False))


def _snapshot_object_name(version_uid: str, bucket: str, filename: str) -> str:
    return "/".join([
        quote(str(version_uid), safe=""),
        quote(str(bucket).lower(), safe=""),
        quote(str(filename), safe=""),
    ])


def _upload_bytes_to_minio(bucket: str, object_name: str, content: bytes) -> None:
    create_bucket(bucket_name=bucket)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name
        upload_object(bucket, object_name, temp_path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def _copy_minio_object(source_bucket: str, source_name: str, target_bucket: str, target_name: str) -> None:
    content = read_object_bytes(source_bucket, source_name)
    _upload_bytes_to_minio(target_bucket, target_name, content)


def _delete_version_file_snapshots(version_uid: str) -> None:
    prefix = f"{quote(str(version_uid), safe='')}/"
    try:
        objects = list_objects(
            bucket_name=VERSION_FILE_SNAPSHOT_BUCKET,
            prefix=prefix,
            recursive=True,
        )
        for obj in list(objects or []):
            object_name = getattr(obj, "object_name", None)
            if object_name:
                remove_object(VERSION_FILE_SNAPSHOT_BUCKET, object_name)
    except Exception as exc:
        print("[neo4j_api.py] Could not clear version file snapshots:", exc)


def _iter_graph_file_entries(graph: dict):
    raw_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            continue
        data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else raw_node
        flow_id = str(raw_node.get("id") or data.get("id") or "").strip()
        if not flow_id:
            continue

        file_list = None
        if isinstance(data.get("file_buckets"), list):
            file_list = data["file_buckets"]
        elif isinstance(data.get("files"), list):
            file_list = data["files"]
        if file_list is None:
            continue

        default_bucket = f"files-step-id-{flow_id}".lower()
        for index, item in enumerate(file_list):
            filename = ""
            bucket = default_bucket
            snapshot_bucket = ""
            snapshot_object = ""
            role = ""
            if isinstance(item, str):
                filename = item.strip()
            elif isinstance(item, dict):
                filename = str(item.get("filename") or item.get("name") or "").strip()
                bucket = str(item.get("bucket") or default_bucket).strip().lower()
                snapshot_bucket = str(item.get("snapshot_bucket") or "").strip().lower()
                snapshot_object = str(item.get("snapshot_object") or "").strip()
                candidate_role = str(item.get("role") or "").strip().lower()
                role = candidate_role if candidate_role in {"code", "data"} else ""
            if not filename:
                continue
            yield file_list, index, item, {
                "filename": filename,
                "bucket": bucket,
                "snapshot_bucket": snapshot_bucket,
                "snapshot_object": snapshot_object,
                "role": role,
            }


def _count_graph_files(graph: dict) -> int:
    return sum(1 for _ in _iter_graph_file_entries(graph))


def _count_graph_files_from_json(graph_json: str | None) -> int:
    if not graph_json:
        return 0
    try:
        graph = json.loads(graph_json)
    except Exception:
        return 0
    return _count_graph_files(graph) if isinstance(graph, dict) else 0


def _set_file_entry_snapshot(file_list: list, index: int, item, file_ref: dict, snapshot_object: str) -> None:
    snapshot_fields = {
        "filename": file_ref["filename"],
        "bucket": file_ref["bucket"],
        "snapshot_bucket": VERSION_FILE_SNAPSHOT_BUCKET,
        "snapshot_object": snapshot_object,
    }
    if isinstance(item, dict):
        item.update(snapshot_fields)
        return
    file_list[index] = snapshot_fields


def _snapshot_version_files(version_uid: str, graph: dict) -> list[dict]:
    snapshots = []
    _delete_version_file_snapshots(version_uid)
    for file_list, index, item, file_ref in _iter_graph_file_entries(graph):
        snapshot_object = _snapshot_object_name(version_uid, file_ref["bucket"], file_ref["filename"])
        snapshot = {
            "filename": file_ref["filename"],
            "bucket": file_ref["bucket"],
            "snapshot_bucket": VERSION_FILE_SNAPSHOT_BUCKET,
            "snapshot_object": snapshot_object,
        }
        try:
            _copy_minio_object(
                file_ref["bucket"],
                file_ref["filename"],
                VERSION_FILE_SNAPSHOT_BUCKET,
                snapshot_object,
            )
            _set_file_entry_snapshot(file_list, index, item, file_ref, snapshot_object)
            snapshot["status"] = "ok"
        except Exception as exc:
            print("[neo4j_api.py] Could not snapshot file object:", file_ref, exc)
            snapshot["status"] = "missing"
            snapshot["error"] = str(exc)
        snapshots.append(snapshot)
    return snapshots


def _restore_version_file_snapshots(graph: dict) -> list[dict]:
    restored = []
    for _, _, _, file_ref in _iter_graph_file_entries(graph):
        snapshot_bucket = file_ref.get("snapshot_bucket")
        snapshot_object = file_ref.get("snapshot_object")
        if not snapshot_bucket or not snapshot_object:
            continue
        result = {
            "filename": file_ref["filename"],
            "bucket": file_ref["bucket"],
            "snapshot_bucket": snapshot_bucket,
            "snapshot_object": snapshot_object,
        }
        try:
            _copy_minio_object(
                snapshot_bucket,
                snapshot_object,
                file_ref["bucket"],
                file_ref["filename"],
            )
            result["status"] = "ok"
        except Exception as exc:
            print("[neo4j_api.py] Could not restore file snapshot:", file_ref, exc)
            result["status"] = "missing"
            result["error"] = str(exc)
        restored.append(result)
    return restored


def _upsert_main_pipeline_version(
    session,
    pipeline_uid: str,
    graph: dict,
    updated_at: str | None = None,
    description: str | None = None,
) -> dict | None:
    graph_with_metadata = _graph_with_metadata(graph, updated_at)
    snapshots = _snapshot_version_files(MAIN_VERSION_UID, graph_with_metadata)
    nodes = graph_with_metadata.get("nodes") if isinstance(graph_with_metadata.get("nodes"), list) else []
    edges = graph_with_metadata.get("edges") if isinstance(graph_with_metadata.get("edges"), list) else []
    file_count = _count_graph_files(graph_with_metadata)
    graph_json = json.dumps(graph_with_metadata, ensure_ascii=False)
    file_snapshots_json = json.dumps(snapshots, ensure_ascii=False)
    record = session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})
    MERGE (v:PIPELINE_VERSION {uid: $main_uid})
    ON CREATE SET v.created_at = datetime()
    SET v.name = $main_name,
        v.version = $main_name,
        v.version_index = 0,
        v.is_main = true,
        v.node_count = $node_count,
        v.edge_count = $edge_count,
        v.file_count = $file_count,
        v.description = CASE
          WHEN $description IS NULL THEN coalesce(v.description, '')
          ELSE $description
        END,
        v.graph_json = $graph_json,
        v.file_snapshots_json = $file_snapshots_json,
        v.updated_at = datetime()
    MERGE (p)-[:HAS_VERSION]->(v)
    SET p.version = $main_name,
        p.description = v.description,
        p.active_version_uid = $main_uid,
        p.updated_at = datetime()
    RETURN
      v.uid AS uid,
      v.name AS name,
      v.version AS version,
      v.version_index AS version_index,
      v.is_main AS is_main,
      v.node_count AS node_count,
      v.edge_count AS edge_count,
      v.file_count AS file_count,
      v.description AS description,
      toString(v.created_at) AS created_at,
      toString(v.updated_at) AS updated_at,
      toString(p.updated_at) AS pipeline_updated_at
    """, pipeline_uid=pipeline_uid,
         main_uid=MAIN_VERSION_UID,
         main_name=MAIN_VERSION_NAME,
         node_count=len(nodes),
         edge_count=len(edges),
         file_count=file_count,
         description=description,
         graph_json=graph_json,
         file_snapshots_json=file_snapshots_json).single()
    if record:
        _sync_version_graph_json_updated_at(
            session,
            MAIN_VERSION_UID,
            record["updated_at"],
            graph_with_metadata,
        )
    return record.data() if record else None


def _upsert_pipeline_version_snapshot(
    session,
    pipeline_uid: str,
    version_uid: str,
    version_name: str,
    graph: dict,
    updated_at: str | None = None,
) -> dict | None:
    if version_uid == MAIN_VERSION_UID:
        return _upsert_main_pipeline_version(session, pipeline_uid, graph, updated_at)

    graph_with_metadata = _graph_with_metadata(graph, updated_at)
    snapshots = _snapshot_version_files(version_uid, graph_with_metadata)
    nodes = graph_with_metadata.get("nodes") if isinstance(graph_with_metadata.get("nodes"), list) else []
    edges = graph_with_metadata.get("edges") if isinstance(graph_with_metadata.get("edges"), list) else []
    file_count = _count_graph_files(graph_with_metadata)
    graph_json = json.dumps(graph_with_metadata, ensure_ascii=False)
    file_snapshots_json = json.dumps(snapshots, ensure_ascii=False)

    record = session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})
    MATCH (v:PIPELINE_VERSION {uid: $version_uid})
    SET v.name = $version_name,
        v.version = $version_name,
        v.is_main = false,
        v.node_count = $node_count,
        v.edge_count = $edge_count,
        v.file_count = $file_count,
        v.graph_json = $graph_json,
        v.file_snapshots_json = $file_snapshots_json,
        v.updated_at = datetime()
    MERGE (p)-[:HAS_VERSION]->(v)
    SET p.version = $version_name,
        p.description = coalesce(v.description, p.description, ''),
        p.active_version_uid = $version_uid,
        p.updated_at = datetime()
    RETURN
      v.uid AS uid,
      v.name AS name,
      v.version AS version,
      v.version_index AS version_index,
      v.is_main AS is_main,
      v.node_count AS node_count,
      v.edge_count AS edge_count,
      v.file_count AS file_count,
      v.description AS description,
      toString(v.created_at) AS created_at,
      toString(v.updated_at) AS updated_at,
      toString(p.updated_at) AS pipeline_updated_at
    """, pipeline_uid=pipeline_uid,
         version_uid=version_uid,
         version_name=version_name,
         node_count=len(nodes),
         edge_count=len(edges),
         file_count=file_count,
         graph_json=graph_json,
         file_snapshots_json=file_snapshots_json).single()
    if record:
        _sync_version_graph_json_updated_at(
            session,
            version_uid,
            record["updated_at"],
            graph_with_metadata,
        )
    return record.data() if record else None


def _empty_pipeline_graph(updated_at: str | None = None) -> dict:
    graph = {
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    if updated_at is not None:
        graph["updated_at"] = updated_at
    return graph


def _json_details(details: Any) -> str:
    try:
        return json.dumps(details or {}, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"details": str(details)}, ensure_ascii=False)


def _short_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _provenance_graph_snapshot(session, pipeline_uid: str) -> dict:
    node_records = list(session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})-[:HAS_STEP]->(step:STEP)
    RETURN
      toString(step.flow_id) AS id,
      coalesce(step.label, '') AS label,
      coalesce(step.type, 'task') AS type,
      coalesce(step.x, 0.0) AS x,
      coalesce(step.y, 0.0) AS y
    ORDER BY
      CASE WHEN toString(step.flow_id) =~ '^[0-9]+$' THEN toInteger(step.flow_id) ELSE 2147483647 END,
      toString(step.flow_id)
    """, pipeline_uid=pipeline_uid))
    edge_records = list(session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})-[:HAS_STEP]->(source:STEP)-[r:FLOWS_TO]->(target:STEP)
    WHERE (p)-[:HAS_STEP]->(target)
    RETURN toString(source.flow_id) AS source,
           toString(target.flow_id) AS target,
           coalesce(r.source_port, '') AS source_port,
           coalesce(r.target_port, '') AS target_port
    ORDER BY source, target, source_port, target_port
    """, pipeline_uid=pipeline_uid))

    node_limit = 60
    edge_limit = 120
    return {
        "node_count": len(node_records),
        "edge_count": len(edge_records),
        "nodes": [
            {
                "id": record["id"],
                "label": _short_text(record["label"], 80),
                "type": normalize_step_type(record["type"]),
                "x": float(record["x"] or 0.0),
                "y": float(record["y"] or 0.0),
            }
            for record in node_records[:node_limit]
        ],
        "edges": [
            {
                "source": record["source"],
                "target": record["target"],
                "source_port": record["source_port"],
                "target_port": record["target_port"],
            }
            for record in edge_records[:edge_limit]
        ],
        "truncated": len(node_records) > node_limit or len(edge_records) > edge_limit,
    }


def _provenance_graph_snapshot_from_graph(graph: dict) -> dict:
    nodes, edges = _parse_visible_graph(graph)
    node_limit = 60
    edge_limit = 120
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": [
            {
                "id": node["props"]["flow_id"],
                "label": _short_text(node["props"].get("label"), 80),
                "type": node["props"].get("type", "task"),
                "x": float(node["props"].get("x") or 0.0),
                "y": float(node["props"].get("y") or 0.0),
            }
            for node in nodes[:node_limit]
        ],
        "edges": [
            {
                "source": edge["source"],
                "target": edge["target"],
                "source_port": edge["source_port"],
                "target_port": edge["target_port"],
            }
            for edge in edges[:edge_limit]
        ],
        "truncated": len(nodes) > node_limit or len(edges) > edge_limit,
    }


def _active_pipeline_version(session, version_uid: str | None = None, version_name: str | None = None) -> dict:
    pipeline_uid = _ensure_design_pipeline(session)
    record = session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})
    OPTIONAL MATCH (p)-[:HAS_VERSION]->(activeVersion:PIPELINE_VERSION {uid: coalesce(p.active_version_uid, $main_uid)})
    RETURN
      p.uid AS pipeline_uid,
      coalesce(p.name, p.label, '') AS pipeline_name,
      coalesce(p.label, p.name, '') AS pipeline_label,
      coalesce(p.active_version_uid, $main_uid) AS active_version_uid,
      CASE
        WHEN coalesce(p.active_version_uid, $main_uid) = $main_uid THEN $main_name
        ELSE coalesce(activeVersion.name, p.version, $main_name)
      END AS active_version_name
    """, pipeline_uid=pipeline_uid,
         main_uid=MAIN_VERSION_UID,
         main_name=MAIN_VERSION_NAME).single()
    resolved_uid = (version_uid or (record["active_version_uid"] if record else MAIN_VERSION_UID) or MAIN_VERSION_UID)
    resolved_name = version_name or (
        MAIN_VERSION_NAME
        if resolved_uid == MAIN_VERSION_UID
        else (record["active_version_name"] if record else MAIN_VERSION_NAME)
    )
    return {
        "pipeline_uid": pipeline_uid,
        "pipeline_name": record["pipeline_name"] if record else "",
        "pipeline_label": record["pipeline_label"] if record else "",
        "version_uid": str(resolved_uid),
        "version_name": str(resolved_name or MAIN_VERSION_NAME),
    }


def _record_provenance_event(
    session,
    action: str,
    actor: str,
    summary: str,
    details: Any = None,
    *,
    version_uid: str | None = None,
    version_name: str | None = None,
) -> None:
    context = _active_pipeline_version(session, version_uid, version_name)
    if isinstance(details, dict):
        event_details = dict(details)
    elif details is None:
        event_details = {}
    else:
        event_details = {"details": details}
    event_details["graph_snapshot"] = _provenance_graph_snapshot(
        session,
        context["pipeline_uid"],
    )
    details_json = _json_details(event_details)
    session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})
    MERGE (v:PIPELINE_VERSION {uid: $version_uid})
    ON CREATE SET v.created_at = datetime(),
                  v.version_index = CASE WHEN $version_uid = $main_uid THEN 0 ELSE null END,
                  v.is_main = CASE WHEN $version_uid = $main_uid THEN true ELSE false END
    SET v.name = CASE
          WHEN $version_uid = $main_uid THEN $main_name
          ELSE coalesce(v.name, $version_name)
        END,
        v.version = CASE
          WHEN $version_uid = $main_uid THEN $main_name
          ELSE coalesce(v.version, $version_name)
        END,
        v.updated_at = coalesce(v.updated_at, datetime())
    MERGE (p)-[:HAS_VERSION]->(v)
    CREATE (event:PROVENANCE_EVENT {
      uid: randomUUID(),
      pipeline_uid: $pipeline_uid,
      version_uid: $version_uid,
      version_name: $version_name,
      actor: $actor,
      action: $action,
      summary: $summary,
      details_json: $details_json,
      created_at: datetime()
    })
    MERGE (p)-[:HAS_PROVENANCE]->(event)
    MERGE (v)-[:HAS_PROVENANCE]->(event)
    """, pipeline_uid=context["pipeline_uid"],
         version_uid=context["version_uid"],
         version_name=context["version_name"],
         main_uid=MAIN_VERSION_UID,
         main_name=MAIN_VERSION_NAME,
         actor=str(actor or "system"),
         action=str(action or "change"),
         summary=str(summary or "Pipeline graph was modified."),
         details_json=details_json)


def _copy_provenance_to_version(session, source_version_uid: str, target_version_uid: str) -> None:
    if not source_version_uid or not target_version_uid or source_version_uid == target_version_uid:
        return
    session.run("""
    MATCH (source:PIPELINE_VERSION {uid: $source_version_uid})
    MATCH (target:PIPELINE_VERSION {uid: $target_version_uid})
    OPTIONAL MATCH (source)-[:HAS_PROVENANCE]->(event:PROVENANCE_EVENT)
    WITH target, collect(event) AS events
    FOREACH (event IN events |
      MERGE (target)-[:HAS_PROVENANCE]->(event)
    )
    """, source_version_uid=source_version_uid, target_version_uid=target_version_uid)


def _clear_pipeline_provenance(session, pipeline_uid: str) -> int:
    record = session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})-[:HAS_PROVENANCE]->(event:PROVENANCE_EVENT)
    RETURN count(DISTINCT event) AS deleted_count
    """, pipeline_uid=pipeline_uid).single()
    deleted_count = int(record["deleted_count"] if record and record["deleted_count"] is not None else 0)
    session.run("""
    MATCH (p:PIPELINE {uid: $pipeline_uid})-[:HAS_PROVENANCE]->(event:PROVENANCE_EVENT)
    WITH DISTINCT event
    DETACH DELETE event
    """, pipeline_uid=pipeline_uid)
    return deleted_count


def _event_details(record) -> dict:
    raw = record["details_json"] if record and record["details_json"] else "{}"
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"details": value}
    except Exception:
        return {"details": raw}


def _mutation_query_type(query_type: str | None) -> bool:
    return str(query_type or "") in {
        "create_pipeline",
        "create_step",
        "configure_flow_step",
        "connect_steps",
        "disconnect_steps",
        "insert_initial_step",
        "insert_between_steps",
        "delete_step",
        "delete_all_steps",
    }


def _provenance_context_from_payload(data: dict) -> dict:
    context = data.get("provenance_context")
    if not isinstance(context, dict):
        return {}
    return {
        "user_query": _short_text(context.get("user_query"), 2000),
        "session_id": _short_text(context.get("session_id"), 120),
    }


# Apply the CORS function to all routes using the after_request decorator
@app.after_request
def apply_cors(response):
    return add_cors_headers(response, request.headers.get("Origin"))

# Adds a pipeline step into Neo4J:
@app.route('/neo4j_add_node', methods=['POST'])
@require_auth
def neo4j_add_node():
    print("[neo4j_api.py] Received query to add STEP node in Neo4j.")
    data = request.json or {}
    properties = data.get("properties", {}) or {}
    step_type = normalize_step_type(properties.get("type"))
    # Set default properties:
    properties["type"] = step_type
    properties.setdefault("label", properties.get("label", ""))
    properties.setdefault("description", properties.get("description", ""))
    properties.setdefault("flow_id", properties.get("flow_id"))
    # Accept both x/y or position:{x,y} if you ever choose to send it that way
    if "position" in properties and isinstance(properties["position"], dict):
        properties.setdefault("x", properties["position"].get("x", 0))
        properties.setdefault("y", properties["position"].get("y", 0))
        properties.pop("position", None)
    properties.setdefault("x", 0)
    properties.setdefault("y", 0)
    properties["ports_json"] = ports_json(properties.pop("ports", None), step_type)
    param_obj = properties.pop("param", {})
    properties["param_json"] = json.dumps(
        param_obj if isinstance(param_obj, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
    )
    properties["secret_params_json"] = secret_params_json(
        properties.pop("secret_params", None),
        param_obj,
    )
    normalize_definition_properties(properties)
    # Normalize to floats (Neo4j-friendly)
    try:
        properties["x"] = float(properties.get("x", 0) or 0)
    except Exception:
        properties["x"] = 0.0
    try:
        properties["y"] = float(properties.get("y", 0) or 0)
    except Exception:
        properties["y"] = 0.0
    if step_type == "destination":
        properties.setdefault("content", "")
    properties.setdefault("has_files", "no")
    # Construct the Cypher query
    query = """
    WITH $props AS props
    OPTIONAL MATCH (candidate:PIPELINE {status:'design'})
    OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
    WITH props, candidate, count(candidateStep) AS step_count
    ORDER BY step_count DESC, candidate.updated_at DESC
    WITH props, collect(candidate)[0] AS candidate
    CALL {
      WITH props, candidate
      WITH props, candidate
      WHERE candidate IS NULL
      CREATE (p:PIPELINE {
          uid:        randomUUID(),
          name:       '',
          label:       '',
          description: '',
          version:    'Main',
          active_version_uid: 'main',
          created_at: datetime(),
          updated_at: datetime(),
          status:     'design'
      })
      RETURN p

      UNION

      WITH props, candidate
      WITH props, candidate
      WHERE candidate IS NOT NULL
      SET candidate.updated_at = datetime()
      RETURN candidate AS p
    }

    WITH props, p
    CREATE (n:STEP)
    SET n += props
    SET n.uid = randomUUID()
    MERGE (p)-[:HAS_STEP]->(n)
    SET p.updated_at = datetime()
    RETURN n, p
    """
    try:
        with driver.session() as session:
            result = session.run(query, {"props": properties})
            record = result.single()
            if record:
                step = record["n"]._properties
                _record_provenance_event(
                    session,
                    "node_created",
                    "manual",
                    f"Created {step.get('type', 'task')} step '{step.get('label', '')}'.",
                    {
                        "flow_id": step.get("flow_id"),
                        "label": step.get("label"),
                        "type": step.get("type"),
                        "description": step.get("description"),
                    },
                )
            return jsonify(record["n"]._properties), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500


# Adds (or updates) a FILE node once a file is added
@app.route('/neo4j_add_file', methods=['POST'])
@require_auth
def neo4j_add_file():
    print("[neo4j_api.py] Received query to add/update FILE node in Neo4j.")
    data = request.json or {}
    properties = data.get("properties", {}) or {}
    # TODO: Use uid instead of flow_id
    flow_id = str(properties.get("flow_id") or "")
    filename = str(properties.get("filename") or "")
    role = str(properties.get("role") or "").strip().lower()
    if role not in {"code", "data"}:
        role = ""
    query = """
    MATCH (n:STEP {flow_id: $flow_id})
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(n)
    SET p.updated_at = datetime()
    MERGE (f:FILE {
      filename: $filename,
      bucket: "files-step-id-" + $flow_id
    })
    SET f.added_at = datetime()
    SET f.uid = randomUUID()
    SET f.role = CASE WHEN $role = '' THEN f.role ELSE $role END
    MERGE (n)-[:HAS_FILE]->(f)
    RETURN n
    """
    try:
        with driver.session() as session:
            result = session.run(query, {"flow_id": flow_id, "filename": filename, "role": role})
            record = result.single()
            if record:
                _record_provenance_event(
                    session,
                    "file_added",
                    "manual",
                    f"Added file '{filename}' to step {flow_id}.",
                    {"flow_id": flow_id, "filename": filename},
                )
            return jsonify(record["n"]._properties), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500

# Removes a FILE node once a file is removed (if several alike - delete most recent)
@app.route('/neo4j_delete_file', methods=['DELETE', 'OPTIONS'])
@require_auth
def neo4j_delete_file():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    print("[neo4j_api.py] Received query to remove FILE node in Neo4j.")
    data = request.json
    properties = data.get("properties", {})
    # TODO: Use uid instead of flow_id
    flow_id = str(properties.get("flow_id"))
    filename = str(properties.get("filename"))
    query = """
    MATCH (n:STEP {flow_id: $flow_id})
    MATCH (n)-[:HAS_FILE]->(f:FILE {filename: $filename})
    WHERE f.bucket = "files-step-id-" + $flow_id
    WITH n, f
    ORDER BY f.added_at DESC
    LIMIT 1
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(n)
    SET p.updated_at = datetime()
    DETACH DELETE f
    RETURN n
    """
    try:
        with driver.session() as session:
            result = session.run(query, {"flow_id": flow_id, "filename": filename})
            record = result.single()
            if record:
                _record_provenance_event(
                    session,
                    "file_removed",
                    "manual",
                    f"Removed file '{filename}' from step {flow_id}.",
                    {"flow_id": flow_id, "filename": filename},
                )
            return jsonify(record["n"]._properties), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500

# Updates a pipeline step in Neo4J:
@app.route('/neo4j_update_node', methods=['POST'])
@require_auth
def neo4j_update_node():
    print("[neo4j_api.py] Received query to update STEP node in Neo4j.")
    data = request.json
    properties = data.get("properties", {}) 
    flow_id = data.get("flow_id") or properties.get("flow_id")
    properties["flow_id"] = flow_id  # Cannot change
    step_type = normalize_step_type(properties.get("type"))
    properties["type"] = step_type  # Cannot change
    # Changes in label/description
    properties["label"] = properties.get("label", "")
    properties["description"] = properties.get("description", "")
    properties["ports_json"] = ports_json(properties.pop("ports", None), step_type)
    param_obj = properties.pop("param", {})
    properties["param_json"] = json.dumps(
        param_obj if isinstance(param_obj, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
    )
    properties["secret_params_json"] = secret_params_json(
        properties.pop("secret_params", None),
        param_obj,
    )
    normalize_definition_properties(properties)
    if step_type == "destination":
        properties["content"] =  properties.get("content", "")
    properties["has_files"] = str(properties.get("has_files") or "no").lower().strip()
    # Ensure we never attempt to store maps / File objects
    properties.pop("files", None)
    # ---- Cypher update ----
    # We match by flow_id (since you send flow_id from the canvas)
    query = """
    MATCH (n:STEP {flow_id: $flow_id})
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(n)
    SET n += $props
    REMOVE n.source_config, n.source_config_json
    SET p.updated_at = datetime()
    RETURN n
    """
    # TODO: Update to UID instead of flow_id once neo4j --> frontend connection is established
    try:
        with driver.session() as session:
            result = session.run(query, {"flow_id": flow_id, "props": properties})
            record = result.single()
            if not record:
                return jsonify({"error": f"[neo4j_api.py] No STEP node found with flow_id={flow_id}"}), 404
            step = record["n"]._properties
            _record_provenance_event(
                session,
                "node_updated",
                "manual",
                f"Updated step {flow_id} '{step.get('label', '')}'.",
                {
                    "flow_id": flow_id,
                    "label": step.get("label"),
                    "type": step.get("type"),
                    "updated_properties": sorted(properties.keys()),
                },
            )
            return jsonify(record["n"]._properties), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_update_generated_artifact', methods=['POST'])
@require_auth
def neo4j_update_generated_artifact():
    data = request.get_json(silent=True) or {}
    flow_id = str(data.get("flow_id") or "").strip()
    generated_artifact = data.get("generated_artifact")
    if not flow_id:
        return jsonify({"error": "flow_id is required"}), 400
    if not isinstance(generated_artifact, dict):
        return jsonify({"error": "generated_artifact must be an object"}), 400

    query = """
    MATCH (n:STEP {flow_id: $flow_id})
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(n)
    SET n.generated_artifact_json = $generated_artifact_json
    SET p.updated_at = datetime()
    RETURN n
    """
    try:
        with driver.session() as session:
            record = session.run(
                query,
                {
                    "flow_id": flow_id,
                    "generated_artifact_json": json.dumps(
                        generated_artifact,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ).single()
            if not record:
                return jsonify(
                    {"error": f"No STEP node found with flow_id={flow_id}"}
                ), 404
            return jsonify(record["n"]._properties), 200
    except Exception as e:
        print("[neo4j_api.py] Error storing generated artifact:", e)
        return jsonify({"error": str(e)}), 500
    
# Deletes all nodes and edges, returns STEP flow_ids deleted
@app.route('/neo4j_clear_nodes', methods=['DELETE', 'OPTIONS'])
@require_auth
def neo4j_clear_nodes():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    print("[neo4j_api.py] Clearing all nodes from Neo4j")
    try:
        with driver.session() as session:
            flow_ids = _clear_active_steps(session)
            if _label_exists(session, "PIPELINE"):
                session.run("""
                MATCH (p:PIPELINE {status:'design'})
                SET p.updated_at = datetime()
                """)
            _record_provenance_event(
                session,
                "graph_cleared",
                "manual",
                f"Cleared {len(flow_ids)} step(s) from the canvas.",
                {"deleted_step_flow_ids": flow_ids},
            )
        return jsonify({
            "status": "ok",
            "message": "All nodes deleted",
            "deleted_step_flow_ids": flow_ids
        }), 200
    except Exception as e:
        print("[neo4j_api.py] Error clearing Neo4j:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_clear_pipeline_workspace', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_clear_pipeline_workspace():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    print("[neo4j_api.py] Clearing pipeline workspace, preserving Main.")
    try:
        with driver.session() as session:
            pipeline_uid = _ensure_design_pipeline(session)
            deleted_step_flow_ids = _clear_active_steps(session)

            deleted_version_record = session.run("""
            MATCH (p:PIPELINE {uid: $pipeline_uid})-[:HAS_VERSION]->(v:PIPELINE_VERSION)
            WHERE coalesce(v.is_main, false) = false AND v.uid <> $main_uid
            RETURN collect(toString(v.uid)) AS uids
            """, pipeline_uid=pipeline_uid, main_uid=MAIN_VERSION_UID).single()
            deleted_version_uids = (
                deleted_version_record["uids"]
                if deleted_version_record and deleted_version_record["uids"]
                else []
            )
            for version_uid in deleted_version_uids:
                _delete_version_file_snapshots(version_uid)

            session.run("""
            MATCH (p:PIPELINE {uid: $pipeline_uid})-[:HAS_VERSION]->(v:PIPELINE_VERSION)
            WHERE coalesce(v.is_main, false) = false AND v.uid <> $main_uid
            DETACH DELETE v
            """, pipeline_uid=pipeline_uid, main_uid=MAIN_VERSION_UID).single()

            sync_result = _sync_graph_to_session(
                session,
                _empty_pipeline_graph(),
                version_name=MAIN_VERSION_NAME,
                active_version_uid=MAIN_VERSION_UID,
            )
            main_version = _upsert_main_pipeline_version(
                session,
                pipeline_uid,
                _empty_pipeline_graph(),
                sync_result.get("updated_at"),
                description="",
            )
            graph = _empty_pipeline_graph(
                main_version.get("updated_at") if isinstance(main_version, dict) else sync_result.get("updated_at")
            )
            deleted_provenance_event_count = _clear_pipeline_provenance(session, pipeline_uid)

        return jsonify({
            "status": "ok",
            "message": "Pipeline workspace cleared",
            "deleted_step_flow_ids": deleted_step_flow_ids,
            "deleted_version_uids": deleted_version_uids,
            "deleted_version_count": len(deleted_version_uids),
            "deleted_provenance_event_count": deleted_provenance_event_count,
            "provenance_cleared": True,
            "version": main_version,
            "graph": graph,
        }), 200
    except Exception as e:
        print("[neo4j_api.py] Error clearing pipeline workspace:", e)
        return jsonify({"error": str(e)}), 500

# Deletes one STEP node and edges:
@app.route('/neo4j_delete_node/<flow_id>', methods=['DELETE', 'OPTIONS'])
@require_auth
def neo4j_delete_node(flow_id):
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    print(f"[neo4j_api.py] Delete STEP node.")
    # TODO: Update to UID instead of flow_id once neo4j --> frontend connection is established
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (s:STEP) RETURN count(s) AS stepCount",
            )
            step_count = result.single()["stepCount"]
            if step_count == 1:
                session.run("""
                MATCH (s:STEP {flow_id: $flow_id})
                OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
                WITH s, collect(DISTINCT f) AS files
                CALL {
                  WITH files
                  UNWIND files AS file
                  WITH file WHERE file IS NOT NULL
                  DETACH DELETE file
                }
                DETACH DELETE s
                """, flow_id=flow_id)
                session.run("""
                MATCH (p:PIPELINE {status:'design'})
                SET p.updated_at = datetime()
                """)
            elif step_count > 1:
                session.run(
                    """
                    MATCH (s:STEP {flow_id: $flow_id})
                    OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
                    WITH s, collect(DISTINCT f) AS files
                    CALL {
                      WITH files
                      UNWIND files AS file
                      WITH file WHERE file IS NOT NULL
                      DETACH DELETE file
                    }
                    DETACH DELETE s
                    """,
                    flow_id=flow_id,
                )
                session.run("""
                MATCH (p:PIPELINE {status:'design'})
                SET p.updated_at = datetime()
                """)
            _record_provenance_event(
                session,
                "node_deleted",
                "manual",
                f"Deleted step {flow_id}.",
                {"flow_id": flow_id},
            )
        return jsonify({"status": "ok", "deleted": flow_id}), 200
    except Exception as e:
        print("[neo4j_api.py] Delete error:", e)
        return jsonify({"error": str(e)}), 500

# Add edge between two steps
@app.route('/neo4j_add_edge', methods=['POST'])
@require_auth
def neo4j_add_edge():
    print("[neo4j_api.py] Received request to add relation FLOWS_TO.")
    data = request.json
    properties = data.get("properties", {})
    from_flow_id = properties.get("flow_id_source")
    to_flow_id = properties.get("flow_id_target")
    source_port = str(properties.get("source_port") or "").strip()
    target_port = str(properties.get("target_port") or "").strip()
    if not from_flow_id or not to_flow_id:
        return jsonify({"error": "Missing from_flow_id or to_flow_id"}), 400
    if not source_port or not target_port:
        return jsonify({"error": "source_port and target_port are required"}), 400
    if str(from_flow_id) == str(to_flow_id):
        return jsonify({"error": "Cannot relate a node to itself"}), 400
    query = """
    MATCH (prev:STEP {flow_id: $from_flow_id})
    MATCH (next:STEP {flow_id: $to_flow_id})
    MERGE (prev)-[r:FLOWS_TO {
      source_port: $source_port,
      target_port: $target_port
    }]->(next)
    WITH prev, next
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(prev)
    SET p.updated_at = datetime()
    RETURN prev.flow_id AS from_flow_id, next.flow_id AS to_flow_id
    """
    try:
        with driver.session() as session:
            record = session.run(query, {
                "from_flow_id": str(from_flow_id),
                "to_flow_id": str(to_flow_id),
                "source_port": source_port,
                "target_port": target_port,
            }).single()
            if not record:
                return jsonify({"error": "STEP node(s) not found for given flow_id(s)"}), 404
            _record_provenance_event(
                session,
                "edge_created",
                "manual",
                f"Connected step {from_flow_id} to step {to_flow_id}.",
                {
                    "from_flow_id": from_flow_id,
                    "to_flow_id": to_flow_id,
                    "source_port": source_port,
                    "target_port": target_port,
                },
            )
            return jsonify({
                "from_flow_id": record["from_flow_id"],
                "to_flow_id": record["to_flow_id"],
                "source_port": source_port or None,
                "target_port": target_port or None,
                "rel_type": "FLOWS_TO"
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j relation query:", e)
        return jsonify({"error": str(e)}), 500
    
# Remove edge between two steps
@app.route('/neo4j_delete_edge', methods=['DELETE', 'OPTIONS'])
@require_auth
def neo4j_delete_edge():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    print("[neo4j_api.py] Received request to add relation FLOWS_TO.")
    data = request.json
    properties = data.get("properties", {})
    from_flow_id = properties.get("flow_id_source")
    to_flow_id = properties.get("flow_id_target")
    source_port = str(properties.get("source_port") or "").strip()
    target_port = str(properties.get("target_port") or "").strip()
    if not from_flow_id or not to_flow_id:
        return jsonify({"error": "Missing from_flow_id or to_flow_id"}), 400
    query = """
    MATCH (prev:STEP {flow_id: $from_flow_id})
    MATCH (next:STEP {flow_id: $to_flow_id})
    OPTIONAL MATCH (prev)-[r:FLOWS_TO]->(next)
    WHERE ($source_port = '' AND $target_port = '')
       OR (coalesce(r.source_port, '') = $source_port
           AND coalesce(r.target_port, '') = $target_port)
       OR (coalesce(r.source_port, '') = ''
           AND coalesce(r.target_port, '') = '')
    WITH prev, next, collect(r) AS relationships, count(r) AS deleted_count
    FOREACH (relationship IN relationships | DELETE relationship)
    WITH prev, next, deleted_count
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(prev)
    SET p.updated_at = datetime()
    RETURN prev.flow_id AS from_flow_id,
           next.flow_id AS to_flow_id,
           deleted_count AS deleted_count
    """
    try:
        with driver.session() as session:
            record = session.run(query, {
                "from_flow_id": str(from_flow_id),
                "to_flow_id": str(to_flow_id),
                "source_port": source_port,
                "target_port": target_port,
            }).single()
            if not record:
                return jsonify({"error": "STEP node(s) not found for given flow_id(s)"}), 404
            _record_provenance_event(
                session,
                "edge_deleted",
                "manual",
                f"Removed connection from step {from_flow_id} to step {to_flow_id}.",
                {
                    "from_flow_id": from_flow_id,
                    "to_flow_id": to_flow_id,
                    "source_port": source_port,
                    "target_port": target_port,
                },
            )
            return jsonify({
                "from_flow_id": record["from_flow_id"],
                "to_flow_id": record["to_flow_id"],
                "deleted_count": record["deleted_count"],
                "rel_type": "FLOWS_TO"
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j relation query:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/neo4j_get_all_files', methods=['GET'])
@require_auth
def neo4j_get_all_files():
    print("[neo4j_api.py] Received request to get all filenames and buckets.")
    query = """
    MATCH (n:STEP)-[:HAS_FILE]->(f:FILE)
    RETURN f.filename AS filename, f.bucket AS bucket, f.role AS role
    """
    try:
        with driver.session() as session:
            result = session.run(query)
            files = [
                {
                    "filename": record["filename"],
                    "bucket": record["bucket"],
                    **({"role": record["role"]} if record["role"] in {"code", "data"} else {}),
                }
                for record in result
            ]
            return jsonify(files), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500
    
@app.route('/neo4j_get_overview_properties', methods=['GET'])
@require_auth
def neo4j_get_overview_properties():
    print("[neo4j_api.py] Received request to get pipeline overview properties.")
    query = """
    MATCH (candidate:PIPELINE {status:'design'})
    OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
    WITH candidate, count(candidateStep) AS step_count
    ORDER BY step_count DESC, candidate.updated_at DESC
    WITH collect(candidate)[0] AS p
    OPTIONAL MATCH (p)-[:HAS_VERSION]->(activeVersion:PIPELINE_VERSION {uid: coalesce(p.active_version_uid, 'main')})
    RETURN 
      CASE
        WHEN coalesce(p.active_version_uid, 'main') = 'main' THEN 'Main'
        ELSE coalesce(activeVersion.name, p.version, 'Main')
      END AS version,
      CASE
        WHEN coalesce(p.active_version_uid, 'main') = 'main' THEN coalesce(activeVersion.description, '')
        ELSE coalesce(activeVersion.description, '')
      END AS description,
      coalesce(p.active_version_uid, 'main') AS active_version_uid,
      toString(coalesce(activeVersion.updated_at, p.updated_at)) AS updated_at,
      toString(coalesce(activeVersion.created_at, p.created_at)) AS created_at
    """
    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE"):
                return jsonify({
                    "version": None,
                    "description": None,
                    "active_version_uid": None,
                    "created_at": None,
                    "updated_at": None
                }), 200
            result = session.run(query)
            record = result.single()
            if record is None:
                return jsonify({
                    "version": None,
                    "description": None,
                    "active_version_uid": None,
                    "created_at": None,
                    "updated_at": None
                }), 200
            return jsonify({
                "version": record["version"],
                "description": record["description"],
                "active_version_uid": record["active_version_uid"],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"]
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_update_pipeline_overview', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_update_pipeline_overview():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    payload = request.get_json(force=True) or {}
    name = str(payload.get("name") or "").strip()
    label = str(payload.get("label") or name).strip()
    version_name = str(payload.get("version") or payload.get("version_name") or "").strip()
    description = str(payload.get("description") or "")
    version_uid = str(payload.get("active_version_uid") or payload.get("version_uid") or "").strip()

    try:
        with driver.session() as session:
            pipeline_uid = _ensure_design_pipeline(session)
            active_record = session.run("""
            MATCH (p:PIPELINE {uid: $pipeline_uid})
            RETURN coalesce(p.active_version_uid, 'main') AS active_version_uid,
                   coalesce(p.version, 'Main') AS pipeline_version
            """, pipeline_uid=pipeline_uid).single()
            active_version_uid = version_uid or (active_record["active_version_uid"] if active_record else MAIN_VERSION_UID)
            fallback_name = active_record["pipeline_version"] if active_record else MAIN_VERSION_NAME
            version_name = MAIN_VERSION_NAME if active_version_uid == MAIN_VERSION_UID else (version_name or fallback_name or MAIN_VERSION_NAME)

            record = session.run("""
            MATCH (p:PIPELINE {uid: $pipeline_uid})
            SET p.version = $version_name,
                p.name = CASE
                  WHEN $name = '' THEN p.name
                  ELSE $name
                END,
                p.label = CASE
                  WHEN $label = '' THEN p.label
                  ELSE $label
                END,
                p.description = $description,
                p.active_version_uid = $active_version_uid,
                p.updated_at = datetime()
            MERGE (v:PIPELINE_VERSION {uid: $active_version_uid})
            ON CREATE SET v.created_at = datetime(),
                          v.version_index = CASE WHEN $active_version_uid = $main_uid THEN 0 ELSE null END,
                          v.is_main = CASE WHEN $active_version_uid = $main_uid THEN true ELSE false END
            SET v.name = $version_name,
                v.version = $version_name,
                v.description = $description,
                v.updated_at = datetime()
            MERGE (p)-[:HAS_VERSION]->(v)
            RETURN
              p.uid AS pipeline_uid,
              coalesce(p.name, '') AS name,
              coalesce(p.label, '') AS label,
              coalesce(v.name, p.version, 'Main') AS version,
              coalesce(v.description, '') AS description,
              v.uid AS active_version_uid,
              toString(v.created_at) AS created_at,
              toString(v.updated_at) AS updated_at
            """, pipeline_uid=pipeline_uid,
                 name=name,
                 label=label,
                 version_name=version_name,
                 description=description,
                 active_version_uid=active_version_uid,
                 main_uid=MAIN_VERSION_UID).single()

            if record:
                _sync_version_graph_json_updated_at(
                    session,
                    active_version_uid,
                    record["updated_at"],
                )
                _record_provenance_event(
                    session,
                    "overview_updated",
                    "manual",
                    f"Updated pipeline overview for version '{record['version']}'.",
                    {
                        "version": record["version"],
                        "description": record["description"],
                    },
                    version_uid=record["active_version_uid"],
                    version_name=record["version"],
                )

            return jsonify(record.data() if record else {
                "version": version_name,
                "description": description,
                "active_version_uid": active_version_uid,
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error updating pipeline overview:", e)
        return jsonify({"error": str(e)}), 500
    
@app.route('/neo4j_get_pipeline_updated_at', methods=['GET'])
@require_auth
def neo4j_get_pipeline_updated_at():
    print("[neo4j_api.py] Received request to get PIPELINE.updated_at")
    query = """
    MATCH (candidate:PIPELINE {status:'design'})
    OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
    WITH candidate, count(candidateStep) AS step_count
    ORDER BY step_count DESC, candidate.updated_at DESC
    RETURN toString(candidate.updated_at) AS updated_at
    LIMIT 1
    """
    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE"):
                return jsonify({"updated_at": None}), 200
            record = session.run(query).single()
            updated_at = record["updated_at"] if record else None
            return jsonify({"updated_at": updated_at}), 200
    except Exception as e:
        print("[neo4j_api.py] Error getting PIPELINE.updated_at:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_list_pipeline_versions', methods=['GET'])
@require_auth
def neo4j_list_pipeline_versions():
    print("[neo4j_api.py] Received request to list pipeline versions.")
    include_graph = str(request.args.get("include_graph") or "").strip().lower() in ("1", "true", "yes")
    query = """
    MATCH (p:PIPELINE {status:'design'})-[:HAS_VERSION]->(v:PIPELINE_VERSION)
    RETURN
      v.uid AS uid,
      CASE WHEN coalesce(v.is_main, false) OR v.uid = $main_uid THEN 'Main' ELSE v.name END AS name,
      CASE WHEN coalesce(v.is_main, false) OR v.uid = $main_uid THEN 'Main' ELSE v.version END AS version,
      v.description AS description,
      v.version_index AS version_index,
      coalesce(v.is_main, false) AS is_main,
      v.node_count AS node_count,
      v.edge_count AS edge_count,
      v.file_count AS file_count,
      v.graph_json AS graph_json,
      toString(v.created_at) AS created_at,
      toString(v.updated_at) AS updated_at
    ORDER BY coalesce(v.is_main, false) DESC, v.created_at DESC
    """
    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE_VERSION"):
                return jsonify({"versions": []}), 200
            result = session.run(query, main_uid=MAIN_VERSION_UID)
            versions = []
            for record in result:
                version = record.data()
                graph_json = version.pop("graph_json", None)
                if version.get("file_count") is None:
                    version["file_count"] = _count_graph_files_from_json(graph_json)
                if include_graph:
                    try:
                        graph = json.loads(graph_json or "{}")
                    except Exception:
                        graph = {}
                    version["graph"] = graph if isinstance(graph, dict) else {}
                versions.append(version)
            return jsonify({"versions": versions}), 200
    except Exception as e:
        print("[neo4j_api.py] Error listing pipeline versions:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_reusable_pipelines', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
@require_auth
def neo4j_reusable_pipelines():
    """List reusable pipelines or save a new immutable reusable-pipeline version."""
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    if request.method == 'DELETE':
        payload = request.get_json(force=True) or {}
        pipeline_uid = str(payload.get("pipeline_uid") or "").strip()
        if not pipeline_uid:
            return jsonify({"error": "pipeline_uid is required"}), 400
        try:
            with driver.session() as session:
                referenced = session.run("""
                CALL {
                  MATCH (step:STEP)
                  WHERE coalesce(step.subpipeline_json, '') CONTAINS $pipeline_uid
                  RETURN count(step) AS parent_reference_count
                }
                CALL {
                  MATCH (:PIPELINE {status:'reusable'})-[:HAS_VERSION]->(version:PIPELINE_VERSION)
                  WHERE coalesce(version.graph_json, '') CONTAINS $pipeline_uid
                  RETURN count(version) AS reusable_reference_count
                }
                RETURN parent_reference_count, reusable_reference_count
                """, pipeline_uid=pipeline_uid).single()
                parent_references = int(referenced["parent_reference_count"] or 0) if referenced else 0
                reusable_references = int(referenced["reusable_reference_count"] or 0) if referenced else 0
                if parent_references or reusable_references:
                    return jsonify({
                        "error": "Reusable pipeline is still referenced and cannot be deleted",
                        "parent_reference_count": parent_references,
                        "reusable_reference_count": reusable_references,
                    }), 409
                record = session.run("""
                MATCH (p:PIPELINE {uid:$pipeline_uid, status:'reusable'})
                OPTIONAL MATCH (p)-[:HAS_VERSION]->(v:PIPELINE_VERSION)
                WITH p, collect(v) AS versions
                DETACH DELETE p
                FOREACH (version IN versions | DETACH DELETE version)
                RETURN size(versions) AS deleted_version_count
                """, pipeline_uid=pipeline_uid).single()
                if not record:
                    return jsonify({"error": "Reusable pipeline not found"}), 404
                return jsonify({
                    "deleted_pipeline_uid": pipeline_uid,
                    "deleted_version_count": record["deleted_version_count"] or 0,
                }), 200
        except Exception as e:
            print("[neo4j_api.py] Error deleting reusable pipeline:", e)
            return jsonify({"error": str(e)}), 500
    if request.method == 'GET':
        try:
            with driver.session() as session:
                if not _label_exists(session, "PIPELINE"):
                    return jsonify({"pipelines": []}), 200
                rows = [record.data() for record in session.run("""
                MATCH (p:PIPELINE {status:'reusable'})-[:HAS_VERSION]->(v:PIPELINE_VERSION)
                RETURN p.uid AS pipeline_uid,
                       coalesce(p.name, p.label, '') AS pipeline_name,
                       coalesce(p.description, '') AS description,
                       p.active_version_uid AS active_version_uid,
                       v.uid AS version_uid,
                       coalesce(v.name, v.version, '') AS version_name,
                       v.interface_json AS interface_json,
                       v.graph_json AS graph_json,
                       v.node_count AS node_count,
                       v.edge_count AS edge_count,
                       toString(v.created_at) AS created_at,
                       toString(v.updated_at) AS updated_at
                ORDER BY pipeline_name, v.created_at DESC
                """)]
                pipelines: dict[str, dict[str, Any]] = {}
                for row in rows:
                    pipeline_uid = str(row.get("pipeline_uid") or "")
                    pipeline = pipelines.setdefault(pipeline_uid, {
                        "uid": pipeline_uid,
                        "name": row.get("pipeline_name") or "",
                        "description": row.get("description") or "",
                        "active_version_uid": row.get("active_version_uid") or "",
                        "versions": [],
                    })
                    try:
                        stored_graph = json.loads(row.get("graph_json") or "{}")
                    except (TypeError, ValueError):
                        stored_graph = {}
                    normalized_graph = normalize_reusable_pipeline_graph(stored_graph)
                    interface = derive_subpipeline_interface(normalized_graph)
                    pipeline["versions"].append({
                        "uid": row.get("version_uid") or "",
                        "name": row.get("version_name") or "",
                        "interface": interface if isinstance(interface, dict) else {},
                        "node_count": row.get("node_count") or 0,
                        "edge_count": row.get("edge_count") or 0,
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                    })
                return jsonify({"pipelines": list(pipelines.values())}), 200
        except Exception as e:
            print("[neo4j_api.py] Error listing reusable pipelines:", e)
            return jsonify({"error": str(e)}), 500

    payload = request.get_json(force=True) or {}
    graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip()
    pipeline_uid = str(payload.get("pipeline_uid") or "").strip()
    version_name = str(payload.get("version_name") or "").strip() or "Version 1"
    if not name:
        return jsonify({"error": "Reusable pipeline name is required"}), 400
    missing_ports = missing_explicit_port_contracts(graph)
    if missing_ports:
        return jsonify({
            "error": "Every reusable-pipeline component requires an explicit typed port contract",
            "components": missing_ports,
        }), 422
    graph = normalize_reusable_pipeline_graph(graph)
    report = validate_pipeline_graph(graph)
    if not report.get("valid"):
        return jsonify({"error": "Reusable pipeline graph is invalid", "validation": report}), 422
    interface = derive_subpipeline_interface(graph)
    if not interface["inputs"] or not interface["outputs"]:
        return jsonify({"error": "Reusable pipeline requires Source and Destination boundaries"}), 422

    version_uid = str(uuid.uuid4())
    graph_with_metadata = _graph_with_metadata(graph, graph.get("updated_at"))
    graph_json = json.dumps(graph_with_metadata, ensure_ascii=False)
    interface_json = json.dumps(interface, ensure_ascii=False, sort_keys=True)
    public_ports_json = json.dumps(public_ports_for_interface(interface), ensure_ascii=False, sort_keys=True)
    try:
        with driver.session() as session:
            if pipeline_uid:
                existing = session.run("""
                MATCH (p:PIPELINE {uid:$pipeline_uid, status:'reusable'})
                RETURN p.uid AS uid
                """, pipeline_uid=pipeline_uid).single()
                if not existing:
                    return jsonify({"error": "Reusable pipeline not found"}), 404
                duplicate_version = session.run("""
                MATCH (:PIPELINE {uid:$pipeline_uid, status:'reusable'})-[:HAS_VERSION]->(version:PIPELINE_VERSION)
                WHERE toLower(trim(coalesce(version.name, ''))) = toLower(trim($version_name))
                RETURN version.uid AS version_uid
                LIMIT 1
                """, pipeline_uid=pipeline_uid, version_name=version_name).single()
                if duplicate_version:
                    return jsonify({
                        "error": "This reusable pipeline already has a version with that name",
                        "version_uid": duplicate_version["version_uid"],
                    }), 409
            else:
                duplicate = session.run("""
                MATCH (existing:PIPELINE {status:'reusable'})
                WHERE toLower(trim(coalesce(existing.name, ''))) = toLower(trim($name))
                RETURN existing.uid AS pipeline_uid,
                       existing.active_version_uid AS version_uid
                LIMIT 1
                """, name=name).single()
                if duplicate:
                    return jsonify({
                        "error": "A reusable pipeline with this name already exists; select it or save a new version",
                        "pipeline_uid": duplicate["pipeline_uid"],
                        "version_uid": duplicate["version_uid"],
                    }), 409
                pipeline_uid = str(uuid.uuid4())
            record = session.run("""
            MERGE (p:PIPELINE {uid:$pipeline_uid})
            ON CREATE SET p.created_at = datetime(), p.status = 'reusable'
            SET p.name = $name,
                p.label = $name,
                p.description = $description,
                p.status = 'reusable',
                p.active_version_uid = $version_uid,
                p.updated_at = datetime()
            WITH p
            OPTIONAL MATCH (p)-[:HAS_VERSION]->(existing:PIPELINE_VERSION)
            WITH p, count(existing) + 1 AS version_index
            CREATE (v:PIPELINE_VERSION {
                uid:$version_uid,
                name:$version_name,
                version:$version_name,
                version_index:version_index,
                is_main:false,
                description:$description,
                graph_json:$graph_json,
                interface_json:$interface_json,
                public_ports_json:$public_ports_json,
                node_count:$node_count,
                edge_count:$edge_count,
                file_count:0,
                created_at:datetime(),
                updated_at:datetime()
            })
            MERGE (p)-[:HAS_VERSION]->(v)
            RETURN p.uid AS pipeline_uid,
                   p.name AS pipeline_name,
                   v.uid AS version_uid,
                   v.name AS version_name,
                   toString(v.created_at) AS created_at
            """,
                pipeline_uid=pipeline_uid,
                name=name,
                description=description,
                version_uid=version_uid,
                version_name=version_name,
                graph_json=graph_json,
                interface_json=interface_json,
                public_ports_json=public_ports_json,
                node_count=len(graph_with_metadata.get("nodes") or []),
                edge_count=len(graph_with_metadata.get("edges") or []),
            ).single()
            saved = record.data() if record else {}
            return jsonify({
                "reference": {
                    "pipeline_uid": saved.get("pipeline_uid") or pipeline_uid,
                    "pipeline_name": saved.get("pipeline_name") or name,
                    "version_uid": saved.get("version_uid") or version_uid,
                    "version_name": saved.get("version_name") or "Version 1",
                },
                "interface": interface,
                "graph": graph_with_metadata,
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error saving reusable pipeline:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_reusable_pipeline_version', methods=['GET'])
@require_auth
def neo4j_reusable_pipeline_version():
    pipeline_uid = str(request.args.get("pipeline_uid") or "").strip()
    version_uid = str(request.args.get("version_uid") or "").strip()
    if not pipeline_uid or not version_uid:
        return jsonify({"error": "pipeline_uid and version_uid are required"}), 400
    try:
        with driver.session() as session:
            record = session.run("""
            MATCH (p:PIPELINE {uid:$pipeline_uid, status:'reusable'})-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid:$version_uid})
            RETURN p.name AS pipeline_name,
                   p.description AS description,
                   v.name AS version_name,
                   v.graph_json AS graph_json,
                   v.interface_json AS interface_json,
                   toString(v.created_at) AS created_at,
                   toString(v.updated_at) AS updated_at
            """, pipeline_uid=pipeline_uid, version_uid=version_uid).single()
            if not record:
                return jsonify({"error": "Reusable pipeline version not found"}), 404
            try:
                graph = json.loads(record["graph_json"] or "{}")
            except (TypeError, ValueError):
                graph = {}
            try:
                interface = json.loads(record["interface_json"] or "{}")
            except (TypeError, ValueError):
                interface = {}
            graph = normalize_reusable_pipeline_graph(graph)
            interface = derive_subpipeline_interface(graph)
            return jsonify({
                "reference": {
                    "pipeline_uid": pipeline_uid,
                    "pipeline_name": record["pipeline_name"] or "",
                    "version_uid": version_uid,
                    "version_name": record["version_name"] or "",
                },
                "description": record["description"] or "",
                "graph": graph if isinstance(graph, dict) else {},
                "interface": interface if isinstance(interface, dict) else {},
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error loading reusable pipeline version:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_attach_reusable_pipeline_version', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_attach_reusable_pipeline_version():
    """Preview or atomically pin one parent Subpipeline to a reusable version."""
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    payload = request.get_json(force=True) or {}
    flow_id = str(payload.get("flow_id") or "").strip()
    pipeline_uid = str(payload.get("pipeline_uid") or "").strip()
    version_uid = str(payload.get("version_uid") or "").strip()
    dry_run = bool(payload.get("dry_run"))
    if not flow_id or not pipeline_uid or not version_uid:
        return jsonify({"error": "flow_id, pipeline_uid, and version_uid are required"}), 400

    try:
        with driver.session() as session:
            record = session.run("""
            MATCH (parent:PIPELINE {status:'design'})-[:HAS_STEP]->(step:STEP {flow_id:$flow_id})
            WHERE step.type = 'subpipeline'
            MATCH (reusable:PIPELINE {uid:$pipeline_uid, status:'reusable'})-[:HAS_VERSION]->(version:PIPELINE_VERSION {uid:$version_uid})
            OPTIONAL MATCH (:STEP)-[incoming:FLOWS_TO]->(step)
            OPTIONAL MATCH (step)-[outgoing:FLOWS_TO]->(:STEP)
            RETURN step.ports_json AS current_ports_json,
                   reusable.name AS pipeline_name,
                   version.name AS version_name,
                   version.graph_json AS graph_json,
                   version.interface_json AS interface_json,
                   version.public_ports_json AS public_ports_json,
                   collect(DISTINCT incoming.target_port) AS connected_inputs,
                   collect(DISTINCT outgoing.source_port) AS connected_outputs
            """,
                flow_id=flow_id,
                pipeline_uid=pipeline_uid,
                version_uid=version_uid,
            ).single()
            if not record:
                return jsonify({"error": "Parent Subpipeline or reusable pipeline version was not found"}), 404

            try:
                graph = normalize_reusable_pipeline_graph(json.loads(record["graph_json"] or "{}"))
                interface = json.loads(record["interface_json"] or "{}")
                public_ports = json.loads(record["public_ports_json"] or "{}")
            except (TypeError, ValueError):
                return jsonify({"error": "Reusable pipeline version has invalid persisted metadata"}), 422
            if not isinstance(interface, dict) or not interface.get("inputs") or not interface.get("outputs"):
                interface = derive_subpipeline_interface(graph)
            if not isinstance(public_ports, dict) or not public_ports.get("inputs") or not public_ports.get("outputs"):
                public_ports = public_ports_for_interface(interface)
            current_ports = normalize_node_ports(record["current_ports_json"], "subpipeline")
            connected_inputs = [str(value or "") for value in (record["connected_inputs"] or []) if value]
            connected_outputs = [str(value or "") for value in (record["connected_outputs"] or []) if value]
            compatibility = plan_subpipeline_port_migration(
                current_ports,
                public_ports,
                connected_inputs,
                connected_outputs,
                requested_inputs=payload.get("input_mapping"),
                requested_outputs=payload.get("output_mapping"),
            )
            reference = {
                "pipeline_uid": pipeline_uid,
                "pipeline_name": record["pipeline_name"] or "",
                "version_uid": version_uid,
                "version_name": record["version_name"] or "",
            }
            response_payload = {
                "reference": reference,
                "interface": interface,
                "graph": graph,
                "ports": public_ports,
                "compatibility": compatibility,
            }
            if dry_run:
                return jsonify(response_payload), 200
            if not compatibility["compatible"]:
                return jsonify({
                    **response_payload,
                    "error": "The selected version requires an explicit connection mapping",
                }), 409

            definition_json = json.dumps({
                "version": 2,
                "reference": reference,
                "interface": interface,
                "expanded": False,
            }, ensure_ascii=False, sort_keys=True)
            ports_value = ports_json(public_ports, "subpipeline")

            def attach(tx):
                updated = tx.run("""
                MATCH (parent:PIPELINE {status:'design'})-[:HAS_STEP]->(step:STEP {flow_id:$flow_id})
                WHERE step.type = 'subpipeline'
                SET step.subpipeline_json = $definition_json,
                    step.ports_json = $ports_json,
                    parent.updated_at = datetime()
                RETURN count(step) AS updated_count
                """,
                    flow_id=flow_id,
                    definition_json=definition_json,
                    ports_json=ports_value,
                ).single()
                if not updated or int(updated["updated_count"] or 0) != 1:
                    raise ValueError("Parent Subpipeline disappeared while attaching the version")
                for old_port, new_port in compatibility["input_mapping"].items():
                    tx.run("""
                    MATCH (:STEP)-[connection:FLOWS_TO]->(step:STEP {flow_id:$flow_id})
                    WHERE step.type = 'subpipeline' AND connection.target_port = $old_port
                    SET connection.target_port = $new_port
                    """, flow_id=flow_id, old_port=old_port, new_port=new_port).consume()
                for old_port, new_port in compatibility["output_mapping"].items():
                    tx.run("""
                    MATCH (step:STEP {flow_id:$flow_id})-[connection:FLOWS_TO]->(:STEP)
                    WHERE step.type = 'subpipeline' AND connection.source_port = $old_port
                    SET connection.source_port = $new_port
                    """, flow_id=flow_id, old_port=old_port, new_port=new_port).consume()

            session.execute_write(attach)
            return jsonify({**response_payload, "attached": True}), 200
    except Exception as e:
        print("[neo4j_api.py] Error attaching reusable pipeline version:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_save_pipeline_version', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_save_pipeline_version():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    payload = request.get_json(force=True) or {}
    graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}

    try:
        with driver.session() as session:
            version_name = str(payload.get("name") or "").strip() or _default_pipeline_version_name(session)
            version_uid = str(uuid.uuid4())
            pipeline_uid = _ensure_design_pipeline(session)
            active_record = session.run("""
            MATCH (p:PIPELINE {uid: $pipeline_uid})
            RETURN coalesce(p.active_version_uid, $main_uid) AS active_version_uid
            """, pipeline_uid=pipeline_uid, main_uid=MAIN_VERSION_UID).single()
            source_version_uid = active_record["active_version_uid"] if active_record else MAIN_VERSION_UID
            graph_with_metadata = _graph_with_metadata(graph, graph.get("updated_at"))
            nodes = graph_with_metadata.get("nodes") if isinstance(graph_with_metadata.get("nodes"), list) else []
            edges = graph_with_metadata.get("edges") if isinstance(graph_with_metadata.get("edges"), list) else []
            file_snapshots = _snapshot_version_files(version_uid, graph_with_metadata)
            file_count = _count_graph_files(graph_with_metadata)
            graph_json = json.dumps(graph_with_metadata, ensure_ascii=False)
            record = session.run("""
            MATCH (p:PIPELINE {uid: $pipeline_uid})
            OPTIONAL MATCH (p)-[:HAS_VERSION]->(existing:PIPELINE_VERSION)
            WITH p, count(CASE WHEN coalesce(existing.is_main, false) = false THEN existing END) + 1 AS version_index
            CREATE (v:PIPELINE_VERSION {
                uid: $version_uid,
                name: $version_name,
                version: $version_name,
                version_index: version_index,
                is_main: false,
                node_count: $node_count,
                edge_count: $edge_count,
                file_count: $file_count,
                description: coalesce(p.description, ''),
                graph_json: $graph_json,
                file_snapshots_json: $file_snapshots_json,
                created_at: datetime(),
                updated_at: datetime()
            })
            MERGE (p)-[:HAS_VERSION]->(v)
            RETURN
              v.uid AS uid,
              v.name AS name,
              v.version AS version,
              v.version_index AS version_index,
              v.is_main AS is_main,
              v.node_count AS node_count,
              v.edge_count AS edge_count,
              v.file_count AS file_count,
              v.description AS description,
              toString(v.created_at) AS created_at,
              toString(v.updated_at) AS updated_at,
              toString(p.updated_at) AS pipeline_updated_at
            """, pipeline_uid=pipeline_uid,
                 version_uid=version_uid,
                 version_name=version_name,
                 node_count=len(nodes),
                 edge_count=len(edges),
                 file_count=file_count,
                 graph_json=graph_json,
                 file_snapshots_json=json.dumps(file_snapshots, ensure_ascii=False)).single()
            if record:
                _sync_version_graph_json_updated_at(
                    session,
                    version_uid,
                    record["updated_at"],
                    graph_with_metadata,
                )
                _copy_provenance_to_version(session, source_version_uid, version_uid)
                _record_provenance_event(
                    session,
                    "version_saved",
                    "manual",
                    f"Saved pipeline snapshot as version '{version_name}'.",
                    {
                        "source_version_uid": source_version_uid,
                        "new_version_uid": version_uid,
                        "node_count": len(nodes),
                        "edge_count": len(edges),
                        "file_count": file_count,
                    },
                    version_uid=version_uid,
                    version_name=version_name,
                )
            return jsonify({"version": record.data() if record else None}), 200
    except Exception as e:
        print("[neo4j_api.py] Error saving pipeline version:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_save_pipeline_main', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_save_pipeline_main():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    payload = request.get_json(force=True) or {}
    graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}

    try:
        with driver.session() as session:
            sync_result = _sync_graph_to_session(
                session,
                graph,
                version_name=MAIN_VERSION_NAME,
                active_version_uid=MAIN_VERSION_UID,
            )
            version = _upsert_main_pipeline_version(
                session,
                sync_result["pipeline_uid"],
                graph,
                sync_result.get("updated_at"),
            )
            return jsonify({"version": version}), 200
    except Exception as e:
        print("[neo4j_api.py] Error saving main pipeline version:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_save_pipeline_active_version', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_save_pipeline_active_version():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    payload = request.get_json(force=True) or {}
    graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
    version_uid = str(payload.get("uid") or payload.get("version_uid") or MAIN_VERSION_UID).strip()
    version_name = str(payload.get("name") or payload.get("version_name") or "").strip()

    try:
        with driver.session() as session:
            pipeline_uid = _ensure_design_pipeline(session)
            if version_uid != MAIN_VERSION_UID and not version_name:
                record = session.run("""
                MATCH (:PIPELINE {uid: $pipeline_uid})-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid: $version_uid})
                RETURN v.name AS name
                """, pipeline_uid=pipeline_uid, version_uid=version_uid).single()
                if not record:
                    return jsonify({"error": f"Pipeline version not found: {version_uid}"}), 404
                version_name = record["name"] or ""

            if version_uid == MAIN_VERSION_UID:
                version = _upsert_main_pipeline_version(session, pipeline_uid, graph, graph.get("updated_at"))
            else:
                version = _upsert_pipeline_version_snapshot(
                    session,
                    pipeline_uid,
                    version_uid,
                    version_name or MAIN_VERSION_NAME,
                    graph,
                    graph.get("updated_at"),
                )

            return jsonify({"version": version}), 200
    except Exception as e:
        print("[neo4j_api.py] Error saving active pipeline version:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_restore_pipeline_version', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_restore_pipeline_version():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    payload = request.get_json(force=True) or {}
    version_uid = str(payload.get("uid") or payload.get("version_uid") or "").strip()
    if not version_uid:
        return jsonify({"error": "Missing version uid"}), 400

    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE_VERSION"):
                return jsonify({"error": "No pipeline versions exist"}), 404
            record = session.run("""
            MATCH (:PIPELINE {status:'design'})-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid: $version_uid})
            RETURN
              v.uid AS uid,
              v.name AS name,
              v.version AS version,
              v.version_index AS version_index,
              coalesce(v.is_main, false) AS is_main,
              v.node_count AS node_count,
              v.edge_count AS edge_count,
              v.file_count AS file_count,
              v.description AS description,
              v.graph_json AS graph_json,
              toString(v.created_at) AS created_at,
              toString(v.updated_at) AS updated_at
            """, version_uid=version_uid).single()
            if not record:
                return jsonify({"error": f"Pipeline version not found: {version_uid}"}), 404
            graph_json = record["graph_json"] or "{}"
            try:
                graph = json.loads(graph_json)
            except Exception:
                graph = {}
            if not isinstance(graph, dict):
                graph = {}
            _sync_version_graph_json_updated_at(
                session,
                record["uid"],
                record["updated_at"],
                graph,
            )
            file_restore = _restore_version_file_snapshots(graph)
            sync_result = _sync_graph_to_session(
                session,
                graph,
                version_name=record["name"],
                active_version_uid=record["uid"],
                touch_pipeline_updated_at=False,
            )
            graph["updated_at"] = record["updated_at"]
            version = {
                "uid": record["uid"],
                "name": record["name"],
                "version": record["version"],
                "version_index": record["version_index"],
                "is_main": record["is_main"],
                "node_count": record["node_count"],
                "edge_count": record["edge_count"],
                "file_count": record["file_count"] if record["file_count"] is not None else _count_graph_files(graph),
                "description": record["description"],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
                "pipeline_updated_at": sync_result.get("updated_at"),
            }
            _record_provenance_event(
                session,
                "version_restored",
                "manual",
                f"Restored version '{record['name']}' to the canvas.",
                {"version_uid": record["uid"], "version_name": record["name"]},
                version_uid=record["uid"],
                version_name=record["name"],
            )
            return jsonify({"version": version, "graph": graph, "file_restore": file_restore}), 200
    except Exception as e:
        print("[neo4j_api.py] Error restoring pipeline version:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_set_pipeline_version_as_main', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_set_pipeline_version_as_main():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    payload = request.get_json(force=True) or {}
    version_uid = str(payload.get("uid") or payload.get("version_uid") or "").strip()
    if not version_uid:
        return jsonify({"error": "Missing version uid"}), 400

    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE_VERSION"):
                return jsonify({"error": "No pipeline versions exist"}), 404
            record = session.run("""
            MATCH (:PIPELINE {status:'design'})-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid: $version_uid})
            RETURN
              v.uid AS uid,
              v.name AS name,
              v.version AS version,
              v.version_index AS version_index,
              coalesce(v.is_main, false) AS is_main,
              v.node_count AS node_count,
              v.edge_count AS edge_count,
              v.file_count AS file_count,
              v.description AS description,
              v.graph_json AS graph_json,
              toString(v.created_at) AS created_at,
              toString(v.updated_at) AS updated_at
            """, version_uid=version_uid).single()
            if not record:
                return jsonify({"error": f"Pipeline version not found: {version_uid}"}), 404

            try:
                graph = json.loads(record["graph_json"] or "{}")
            except Exception:
                graph = {}
            if not isinstance(graph, dict):
                graph = {}
            _sync_version_graph_json_updated_at(
                session,
                record["uid"],
                record["updated_at"],
                graph,
            )

            file_restore = _restore_version_file_snapshots(graph)
            sync_result = _sync_graph_to_session(
                session,
                graph,
                version_name=MAIN_VERSION_NAME,
                active_version_uid=MAIN_VERSION_UID,
            )
            graph["updated_at"] = sync_result.get("updated_at")
            main_version = _upsert_main_pipeline_version(
                session,
                sync_result["pipeline_uid"],
                graph,
                sync_result.get("updated_at"),
                description=record["description"] or "",
            )
            source_version = {
                "uid": record["uid"],
                "name": record["name"],
                "version": record["version"],
                "version_index": record["version_index"],
                "is_main": record["is_main"],
                "node_count": record["node_count"],
                "edge_count": record["edge_count"],
                "file_count": record["file_count"] if record["file_count"] is not None else _count_graph_files(graph),
                "description": record["description"],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
            }
            _copy_provenance_to_version(session, record["uid"], MAIN_VERSION_UID)
            _record_provenance_event(
                session,
                "main_version_updated",
                "manual",
                f"Promoted version '{record['name']}' to Main.",
                {"source_version_uid": record["uid"], "source_version_name": record["name"]},
                version_uid=MAIN_VERSION_UID,
                version_name=MAIN_VERSION_NAME,
            )
            return jsonify({
                "version": main_version,
                "source_version": source_version,
                "graph": graph,
                "file_restore": file_restore,
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error setting pipeline version as Main:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_delete_pipeline_version', methods=['DELETE', 'OPTIONS'])
@require_auth
def neo4j_delete_pipeline_version():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    payload = request.get_json(silent=True) or {}
    version_uid = str(
        payload.get("uid")
        or payload.get("version_uid")
        or request.args.get("uid")
        or request.args.get("version_uid")
        or ""
    ).strip()
    if not version_uid:
        return jsonify({"error": "Missing version uid"}), 400

    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE_VERSION"):
                return jsonify({"error": "No pipeline versions exist"}), 404
            candidate = session.run("""
            MATCH (:PIPELINE {status:'design'})-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid: $version_uid})
            RETURN coalesce(v.is_main, false) AS is_main
            """, version_uid=version_uid).single()
            if not candidate:
                return jsonify({"error": f"Pipeline version not found: {version_uid}"}), 404
            if candidate["is_main"]:
                return jsonify({"error": "Main pipeline version cannot be deleted"}), 400
            _delete_version_file_snapshots(version_uid)
            record = session.run("""
            MATCH (p:PIPELINE {status:'design'})-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid: $version_uid})
            WITH p, v, v.name AS deleted_name, p.active_version_uid = $version_uid AS deleted_was_active
            DETACH DELETE v
            WITH p, deleted_name, deleted_was_active
            CALL {
              WITH p, deleted_name
              OPTIONAL MATCH (p)-[:HAS_VERSION]->(remaining:PIPELINE_VERSION)
              RETURN
                count(remaining) AS remaining_count,
                sum(CASE WHEN remaining.name = deleted_name THEN 1 ELSE 0 END) AS same_name_remaining
            }
            OPTIONAL MATCH (p)-[:HAS_VERSION]->(mainVersion:PIPELINE_VERSION {uid: $main_uid})
            SET p.version = CASE
                WHEN p.version <> deleted_name THEN p.version
                WHEN same_name_remaining > 0 THEN p.version
                ELSE 'Main'
              END,
              p.active_version_uid = CASE
                WHEN p.active_version_uid <> $version_uid THEN p.active_version_uid
                ELSE 'main'
              END,
              p.description = CASE
                WHEN deleted_was_active THEN coalesce(mainVersion.description, '')
                ELSE p.description
              END,
              p.updated_at = datetime()
            RETURN
              deleted_name,
              remaining_count,
              p.version AS pipeline_version,
              toString(p.updated_at) AS pipeline_updated_at
            """, version_uid=version_uid, main_uid=MAIN_VERSION_UID).single()
            if not record:
                return jsonify({"error": f"Pipeline version not found: {version_uid}"}), 404
            _record_provenance_event(
                session,
                "version_deleted",
                "manual",
                f"Deleted saved version '{record['deleted_name']}'.",
                {
                    "deleted_uid": version_uid,
                    "deleted_name": record["deleted_name"],
                    "remaining_count": record["remaining_count"],
                },
            )
            return jsonify({
                "deleted_uid": version_uid,
                "deleted_name": record["deleted_name"],
                "remaining_count": record["remaining_count"],
                "pipeline_version": record["pipeline_version"],
                "pipeline_updated_at": record["pipeline_updated_at"],
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error deleting pipeline version:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_get_provenance_events', methods=['GET'])
@require_auth
def neo4j_get_provenance_events():
    requested_version_uid = str(request.args.get("version_uid") or "").strip()
    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE"):
                return jsonify({
                    "pipeline": None,
                    "version": {"uid": requested_version_uid or MAIN_VERSION_UID, "name": MAIN_VERSION_NAME},
                    "events": [],
                }), 200

            pipeline_record = session.run("""
            MATCH (candidate:PIPELINE {status:'design'})
            OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
            WITH candidate, count(candidateStep) AS step_count
            ORDER BY step_count DESC, candidate.updated_at DESC
            WITH collect(candidate)[0] AS p
            RETURN
              p.uid AS uid,
              coalesce(p.name, '') AS name,
              coalesce(p.label, '') AS label,
              coalesce(p.active_version_uid, $main_uid) AS active_version_uid,
              toString(p.created_at) AS created_at,
              toString(p.updated_at) AS updated_at
            """, main_uid=MAIN_VERSION_UID).single()
            if not pipeline_record:
                return jsonify({
                    "pipeline": None,
                    "version": {"uid": requested_version_uid or MAIN_VERSION_UID, "name": MAIN_VERSION_NAME},
                    "events": [],
                }), 200

            version_uid = requested_version_uid or pipeline_record["active_version_uid"] or MAIN_VERSION_UID
            version_record = session.run("""
            MATCH (p:PIPELINE {uid: $pipeline_uid})
            OPTIONAL MATCH (p)-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid: $version_uid})
            RETURN
              coalesce(v.uid, $version_uid) AS uid,
              CASE WHEN coalesce(v.uid, $version_uid) = $main_uid THEN $main_name ELSE coalesce(v.name, $main_name) END AS name,
              coalesce(v.description, '') AS description,
              toString(v.created_at) AS created_at,
              toString(v.updated_at) AS updated_at,
              v.graph_json AS graph_json
            """, pipeline_uid=pipeline_record["uid"],
                 version_uid=version_uid,
                 main_uid=MAIN_VERSION_UID,
                 main_name=MAIN_VERSION_NAME).single()

            version_payload = version_record.data() if version_record else {
                "uid": version_uid,
                "name": MAIN_VERSION_NAME if version_uid == MAIN_VERSION_UID else version_uid,
            }
            graph_json = version_payload.pop("graph_json", None)
            try:
                version_graph = json.loads(graph_json or "{}")
            except (TypeError, ValueError):
                version_graph = {}
            if isinstance(version_graph, dict) and (
                isinstance(version_graph.get("nodes"), list)
                or isinstance(version_graph.get("edges"), list)
            ):
                current_graph_snapshot = _provenance_graph_snapshot_from_graph(version_graph)
            elif version_uid == pipeline_record["active_version_uid"]:
                current_graph_snapshot = _provenance_graph_snapshot(session, pipeline_record["uid"])
            else:
                current_graph_snapshot = _provenance_graph_snapshot_from_graph({})

            events = []
            if _label_exists(session, "PROVENANCE_EVENT"):
                result = session.run("""
                MATCH (:PIPELINE_VERSION {uid: $version_uid})-[:HAS_PROVENANCE]->(event:PROVENANCE_EVENT)
                RETURN
                  event.uid AS uid,
                  event.actor AS actor,
                  event.action AS action,
                  event.summary AS summary,
                  event.details_json AS details_json,
                  event.version_uid AS version_uid,
                  event.version_name AS version_name,
                  toString(event.created_at) AS created_at
                ORDER BY event.created_at ASC, event.uid ASC
                """, version_uid=version_uid)
                for record in result:
                    events.append({
                        "uid": record["uid"],
                        "actor": record["actor"],
                        "action": record["action"],
                        "summary": record["summary"],
                        "details": _event_details(record),
                        "version_uid": record["version_uid"],
                        "version_name": record["version_name"],
                        "created_at": record["created_at"],
                    })

            return jsonify({
                "pipeline": {
                    "uid": pipeline_record["uid"],
                    "name": pipeline_record["name"],
                    "label": pipeline_record["label"],
                    "active_version_uid": pipeline_record["active_version_uid"],
                    "created_at": pipeline_record["created_at"],
                    "updated_at": pipeline_record["updated_at"],
                },
                "version": version_payload,
                "events": events,
                "current_graph_snapshot": current_graph_snapshot,
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error loading provenance events:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_record_provenance_event', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_record_provenance_event():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    payload = request.get_json(force=True) or {}
    try:
        with driver.session() as session:
            _record_provenance_event(
                session,
                str(payload.get("action") or "change"),
                str(payload.get("actor") or "system"),
                str(payload.get("summary") or "Pipeline graph was modified."),
                payload.get("details") if isinstance(payload.get("details"), dict) else {},
                version_uid=str(payload.get("version_uid") or "").strip() or None,
                version_name=str(payload.get("version_name") or "").strip() or None,
            )
        return jsonify({"ok": True}), 200
    except Exception as e:
        print("[neo4j_api.py] Error recording provenance event:", e)
        return jsonify({"error": str(e)}), 500

# (Internal) Run query by LLM
@app.route('/neo4j_run_query', methods=['POST'])
@require_auth
def neo4j_run_query():
    data = request.json
    query = data['query']
    query_type = data.get("query_type")
    print("[neo4j_api.py] Received query to execute in Neo4J:", query)
    with driver.session() as session:
        session_result = session.run(query)
        # We are assuming that the query returns something to jsonify
        results = [record.data() for record in session_result]
        if _mutation_query_type(query_type):
            provenance_context = _provenance_context_from_payload(data)
            details = {
                "query_type": query_type,
                **provenance_context,
                "result": _short_text(results, 2000),
            }
            _record_provenance_event(
                session,
                str(query_type),
                "agent",
                f"Agent executed pipeline operation '{query_type}'.",
                details,
            )
        return jsonify(results)

@app.route("/neo4j_update_node_position", methods=["POST"])
@require_auth
def neo4j_update_node_position():
    payload = request.get_json(force=True) or {}
    flow_id = str(payload.get("flow_id") or "")
    x = payload.get("x")
    y = payload.get("y")
    if not flow_id:
        return jsonify({"error": "Missing flow_id"}), 400
    query = """
    MATCH (s:STEP {flow_id: $flow_id})
    SET s.x = $x,
        s.y = $y
    WITH s
    MATCH (p:PIPELINE)-[:HAS_STEP]->(s)
    SET p.updated_at = datetime()
    RETURN s.flow_id AS flow_id
    """
    try:
        with driver.session() as session:
            session.run(query, flow_id=flow_id, x=x, y=y)
            _record_provenance_event(
                session,
                "node_moved",
                "manual",
                f"Moved step {flow_id} on the canvas.",
                {"flow_id": flow_id, "x": x, "y": y},
            )
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_sync_graph', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_sync_graph():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    payload = request.get_json(force=True) or {}
    graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
    active_version_uid = str(payload.get("active_version_uid") or payload.get("version_uid") or MAIN_VERSION_UID).strip() or MAIN_VERSION_UID
    version_name = str(payload.get("version_name") or payload.get("active_version_name") or "").strip()

    try:
        with driver.session() as session:
            pipeline_uid = _ensure_design_pipeline(session)
            if active_version_uid == MAIN_VERSION_UID:
                version_name = MAIN_VERSION_NAME
            elif not version_name:
                record = session.run("""
                MATCH (:PIPELINE {uid: $pipeline_uid})-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid: $version_uid})
                RETURN v.name AS name
                """, pipeline_uid=pipeline_uid, version_uid=active_version_uid).single()
                if not record:
                    return jsonify({"error": f"Pipeline version not found: {active_version_uid}"}), 404
                version_name = record["name"] or MAIN_VERSION_NAME

            result = session.execute_write(
                lambda tx: _sync_graph_to_session(
                    tx,
                    graph,
                    version_name=version_name,
                    active_version_uid=active_version_uid,
                )
            )
        return jsonify(result), 200
    except Exception as e:
        print("[neo4j_api.py] Error syncing visible graph:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_restore_graph_history', methods=['POST', 'OPTIONS'])
@require_auth
def neo4j_restore_graph_history():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    payload = request.get_json(force=True) or {}
    graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
    direction = str(payload.get("direction") or "").strip().lower()
    if direction not in {"undo", "redo"}:
        return jsonify({"error": "direction must be 'undo' or 'redo'"}), 400
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}

    try:
        with driver.session() as session:
            def restore_history(tx):
                result = _sync_graph_to_session(tx, graph)
                action = f"{direction}_applied"
                summary = (
                    "Restored the previous graph snapshot with Undo."
                    if direction == "undo"
                    else "Restored the next graph snapshot with Redo."
                )
                _record_provenance_event(
                    tx,
                    action,
                    "manual",
                    summary,
                    {
                        **details,
                        "direction": direction,
                        "restored_node_count": result.get("node_count", 0),
                        "restored_edge_count": result.get("edge_count", 0),
                    },
                )
                return result, action

            result, action = session.execute_write(restore_history)
        return jsonify({
            **result,
            "provenance_action": action,
        }), 200
    except Exception as e:
        print("[neo4j_api.py] Error restoring graph history:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_get_graph', methods=['GET'])
@require_auth
def neo4j_get_graph():
    print("[neo4j_api.py] Received request to get graph (ReactFlow export-like).")
    query = """
    MATCH (candidate:PIPELINE {status:'design'})
    OPTIONAL MATCH (candidate)-[:HAS_STEP]->(candidateStep:STEP)
    WITH candidate, count(candidateStep) AS step_count
    ORDER BY step_count DESC, candidate.updated_at DESC
    WITH collect({pipeline: candidate, step_count: step_count}) AS ranked,
         count(candidate) AS design_pipeline_count
    WITH ranked[0].pipeline AS p,
         ranked[0].step_count AS pipeline_step_count,
         design_pipeline_count
    OPTIONAL MATCH (p)-[:HAS_STEP]->(s:STEP)
    OPTIONAL MATCH (s)-[r:FLOWS_TO]->(t:STEP)
    OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
    WITH
      p,
      design_pipeline_count,
      pipeline_step_count,
      s,
      t,
      r,
      collect(DISTINCT f { .filename, .bucket, .role, added_at: toString(f.added_at) }) AS files_for_step
    RETURN
      toString(p.updated_at) AS updated_at,
      p {
        .uid,
        .name,
        .label,
        .description,
        .version,
        .active_version_uid,
        .settings_json,
        .status,
        created_at: toString(p.created_at),
        updated_at: toString(p.updated_at)
      } AS pipeline,
      design_pipeline_count,
      pipeline_step_count,
      collect(DISTINCT {
        step: s,
        files: files_for_step
      }) AS step_rows,
      collect(DISTINCT {
        source: s.flow_id,
        target: t.flow_id,
        source_port: r.source_port,
        target_port: r.target_port
      }) AS flows
    """

    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE"):
                return jsonify({
                    "updated_at": None,
                    "nodes": [],
                    "edges": [],
                    "viewport": {"x": 0, "y": 0, "zoom": 1}
                }), 200
            record = session.run(query).single()
            if not record:
                return jsonify({
                    "updated_at": None,
                    "nodes": [],
                    "edges": [],
                    "viewport": {"x": 0, "y": 0, "zoom": 1}
                }), 200

            updated_at = record["updated_at"]
            pipeline = record["pipeline"] or {}
            settings = {}
            if isinstance(pipeline, dict):
                settings_json = pipeline.get("settings_json")
                if isinstance(settings_json, str) and settings_json.strip():
                    try:
                        parsed_settings = json.loads(settings_json)
                        settings = parsed_settings if isinstance(parsed_settings, dict) else {}
                    except Exception:
                        settings = {}
                pipeline["design_pipeline_count"] = record["design_pipeline_count"]
                pipeline["step_count"] = record["pipeline_step_count"]
                active_version_uid = pipeline.get("active_version_uid")
                if isinstance(active_version_uid, str) and active_version_uid.strip():
                    active_version_record = session.run("""
                    MATCH (:PIPELINE {status:'design'})-[:HAS_VERSION]->(v:PIPELINE_VERSION {uid: $active_version_uid})
                    RETURN v.name AS name
                    """, active_version_uid=active_version_uid.strip()).single()
                    pipeline["active_version_name"] = active_version_record["name"] if active_version_record else None
            step_rows = record["step_rows"] or []
            flows = record["flows"] or []

            nodes = []
            for row in step_rows:
                s = row.get("step") if row else None
                if s is None:
                    continue

                props = dict(s.items())
                flow_id = props.get("flow_id")
                if flow_id is None:
                    continue

                node_id = str(flow_id)

                # position
                try:
                    x = float(props.get("x", 0) or 0)
                except Exception:
                    x = 0.0
                try:
                    y = float(props.get("y", 0) or 0)
                except Exception:
                    y = 0.0

                step_kind = normalize_step_type(props.get("type"), default="task")

                files_for_step = row.get("files") or []
                # filenames list (simple)
                filenames = [
                    f.get("filename")
                    for f in files_for_step
                    if isinstance(f, dict) and f.get("filename")
                ]

                data = {
                    "label": props.get("label", ""),
                    "description": props.get("description", ""),
                    "type": step_kind,
                    "template_label": props.get("template_label", ""),
                    "ports": normalize_node_ports(props.get("ports_json"), step_kind),

                    # add files so polling doesn't wipe them
                    "files": filenames,

                    # optional richer info (bucket + added_at)
                    "file_buckets": files_for_step,
                }

                if step_kind == "destination" and "content" in props:
                    data["content"] = props.get("content") or ""
                if "has_files" in props:
                    data["has_files"] = props.get("has_files")
                if "endpoint" in props:
                    data["endpoint"] = props.get("endpoint")
                if "database" in props:
                    data["database"] = props.get("database")
                if "param_json" in props:
                    param_json = props.get("param_json")
                    data["param_json"] = param_json
                    try:
                        parsed_param = json.loads(param_json) if isinstance(param_json, str) else {}
                    except Exception:
                        parsed_param = {}
                    data["param"] = parsed_param if isinstance(parsed_param, dict) else {}
                    data["secret_params"] = normalize_secret_param_keys(
                        props.get("secret_params_json"),
                        data["param"],
                    )
                data.update(definition_data_from_properties(props))
                if step_kind == "subpipeline" and isinstance(data.get("subpipeline"), dict):
                    subpipeline = dict(data["subpipeline"])
                    reference = subpipeline.get("reference")
                    reference = reference if isinstance(reference, dict) else {}
                    referenced_pipeline_uid = str(reference.get("pipeline_uid") or "").strip()
                    referenced_version_uid = str(reference.get("version_uid") or "").strip()
                    if referenced_pipeline_uid and referenced_version_uid:
                        referenced = session.run("""
                        MATCH (rp:PIPELINE {uid:$pipeline_uid, status:'reusable'})-[:HAS_VERSION]->(rv:PIPELINE_VERSION {uid:$version_uid})
                        RETURN rp.name AS pipeline_name,
                               rv.name AS version_name,
                               rv.graph_json AS graph_json,
                               rv.interface_json AS interface_json
                        """, pipeline_uid=referenced_pipeline_uid, version_uid=referenced_version_uid).single()
                        if referenced:
                            try:
                                resolved_graph = json.loads(referenced["graph_json"] or "{}")
                            except (TypeError, ValueError):
                                resolved_graph = {}
                            resolved_graph = normalize_reusable_pipeline_graph(resolved_graph)
                            resolved_interface = derive_subpipeline_interface(resolved_graph)
                            subpipeline["reference"] = {
                                **reference,
                                "pipeline_name": referenced["pipeline_name"] or reference.get("pipeline_name") or "",
                                "version_name": referenced["version_name"] or reference.get("version_name") or "",
                            }
                            subpipeline["interface"] = resolved_interface
                            subpipeline["resolved_graph"] = resolved_graph
                        else:
                            subpipeline["resolution_error"] = "Referenced reusable pipeline version was not found."
                    data["subpipeline"] = subpipeline

                nodes.append({
                    "id": node_id,
                    "type": "custom",
                    "position": {"x": x, "y": y},
                    "data": data,
                })

            edges = []
            node_connection_profiles_by_id = {
                node["id"]: (
                    node["data"]["type"],
                    node["data"].get("template_label") or "",
                )
                for node in nodes
            }
            for f in flows:
                src = f.get("source") if isinstance(f, dict) else None
                tgt = f.get("target") if isinstance(f, dict) else None
                if src is None or tgt is None:
                    continue
                src = str(src)
                tgt = str(tgt)
                source_profile = node_connection_profiles_by_id.get(src, ("task", ""))
                target_profile = node_connection_profiles_by_id.get(tgt, ("task", ""))
                source_port = f.get("source_port") or default_output_port_id(*source_profile)
                target_port = f.get("target_port") or default_input_port_id(*target_profile)
                edges.append({
                    "id": (
                        f"reactflow__edge-{src}-{source_port or 'default'}-"
                        f"{tgt}-{target_port or 'default'}"
                    ),
                    "source": src,
                    "target": tgt,
                    "sourceHandle": source_port or None,
                    "targetHandle": target_port or None,
                })

            return jsonify({
                "updated_at": updated_at,
                "pipeline": pipeline,
                "settings": settings,
                "nodes": nodes,
                "edges": edges,
                "viewport": {"x": 0, "y": 0, "zoom": 1}
            }), 200

    except Exception as e:
        print("[neo4j_api.py] Error executing neo4j_get_graph:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/neo4j_delete_node/<uid>', methods=['DELETE', 'OPTIONS'])
@require_auth
def delete_node(uid):
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    query = """
        MATCH (n:STEP { uid: $uid })
        DETACH DELETE n
    """
    print(f"[neo4j_api.py] Deleting node with uid={uid}")
    try:
        with driver.session() as session:
            session.run(query, {"uid": uid})
        return jsonify({"status": f"Node with uid={uid} deleted"}), 200
    except Exception as e:
        print("[neo4j_api.py] Error deleting node:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/neo4j_update_name', methods=['POST'])
@require_auth
def neo4j_update_name():
    data = request.json
    # Extracting fields from the request
    name = data.get("name", "")
    uid = data.get("uid", "")
   
    # Construct the Cypher query
    query = """
        MATCH (d:STEP { uid: $uid })
        SET d.name = $name
        RETURN d
    """
    print("[neo4j_api.py] Received query to execute in Neo4j:\n", query)
    try:
        with driver.session() as session:
            session_result = session.run(query, {"uid": uid, "name": name})
            results = [record["d"] for record in session_result] 
            return jsonify([dict(r) for r in results]), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500
    

@app.route('/neo4j_update_description', methods=['POST'])
@require_auth
def neo4j_update_description():
    data = request.json
    # Extracting fields from the request
    description = data.get("description", "")
    uid = data.get("uid", "")
   
    # Construct the Cypher query 
    query = """
        MATCH (d:STEP { uid: $uid })
        SET d.description = $description
        RETURN d
    """
    print("[neo4j_api.py] Received query to execute in Neo4j:\n", query)
    try:
        with driver.session() as session:
            session_result = session.run(query, {"uid": uid, "description": description})
            results = [record["d"] for record in session_result] 
            return jsonify([dict(r) for r in results]), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500
