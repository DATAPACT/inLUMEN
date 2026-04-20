from flask import Flask, request, jsonify
from neo4j import GraphDatabase
from auth_middleware import require_auth
import uuid
import configparser
import json
import os

CORS_ALLOWED_ORIGIN = os.getenv("CORS_ALLOWED_ORIGIN", "http://localhost:8080")

config_neo4j = configparser.ConfigParser()
config_neo4j.read('neo4j_config.ini')

# Global graph data
graph_data = []

app = Flask(__name__)

driver = GraphDatabase.driver(config_neo4j.get('neo4j','uri'), auth=(config_neo4j.get('neo4j','username'), config_neo4j.get('neo4j','password')))


def _label_exists(session, label_name: str) -> bool:
    result = session.run("CALL db.labels() YIELD label RETURN collect(label) AS labels").single()
    labels = result["labels"] if result and result["labels"] else []
    return label_name in labels

# Define a function to set the CORS headers
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = CORS_ALLOWED_ORIGIN
    response.headers['Access-Control-Allow-Methods'] = 'OPTIONS, GET, POST, DELETE'  # Adjust as needed
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# Apply the CORS function to all routes using the after_request decorator
@app.after_request
def apply_cors(response):
    return add_cors_headers(response)

# Adds a pipeline step into Neo4J:
@app.route('/neo4j_add_node', methods=['POST'])
@require_auth
def neo4j_add_node():
    print("[neo4j_api.py] Received query to add STEP node in Neo4j.")
    data = request.json or {}
    properties = data.get("properties", {}) or {}
    step_type = str(properties.get("type") or "").lower().strip()
    # Set default properties:
    properties.setdefault("type", step_type)
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
    # Normalize to floats (Neo4j-friendly)
    try:
        properties["x"] = float(properties.get("x", 0) or 0)
    except Exception:
        properties["x"] = 0.0
    try:
        properties["y"] = float(properties.get("y", 0) or 0)
    except Exception:
        properties["y"] = 0.0
    # Add type-specific properties:
    if step_type == "input":
        properties.setdefault("content", "")
        properties.setdefault("has_files", "no")
    elif step_type == "config":
        properties.setdefault("param_json", json.dumps(properties.get("param", {})))
        properties.pop("param", None)
    elif step_type == "action":
        properties.setdefault("has_files", "no")
    elif step_type == "storage":
        properties.setdefault("endpoint", "")
        properties.setdefault("database", "minio")
    elif step_type == "api":
        properties.setdefault("endpoint", "")
    elif step_type == "output":
        properties.setdefault("content", "")
        properties.setdefault("has_files", "no")
    elif step_type == "custom":
        properties.setdefault("has_files", "no")
    # Construct the Cypher query
    query = """
    WITH $props AS props
    OPTIONAL MATCH (s:STEP)
    WITH props, count(s) AS stepCount

    CALL (stepCount) {
      WITH stepCount
      WHERE stepCount = 0
      CREATE (p:PIPELINE {
          uid:        randomUUID(),
          label:       '',
          description: '',
          version:    '1.1',
          created_at: datetime(),
          updated_at: datetime(),
          status:     'design'
      })
      RETURN p

      UNION

      WITH stepCount
      WHERE stepCount <> 0
      MERGE (p:PIPELINE {status: 'design'})
      ON CREATE SET
          p.uid = randomUUID(),
          p.label = '',
          p.description = '',
          p.version = '1.1',
          p.created_at = datetime(),
          p.updated_at = datetime()
      ON MATCH SET
          p.updated_at = datetime()
      RETURN p
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
    MERGE (n)-[:HAS_FILE]->(f)
    RETURN n
    """
    try:
        with driver.session() as session:
            result = session.run(query, {"flow_id": flow_id, "filename": filename})
            record = result.single()
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
    step_type = str(properties.get("type", "")).lower().strip()
    properties["type"] = step_type  # Cannot change
    # Changes in label/description
    properties["label"] = properties.get("label", "")
    properties["description"] = properties.get("description", "")
    # Changes specific to config step type:
    if step_type == "config":
        # Convert param dict -> JSON string
        param_obj = properties.get("param")
        if not isinstance(param_obj, dict):
            param_obj = {}
        properties["param_json"] = json.dumps(param_obj)
        properties.pop("param", None)
    # Changes specific to input/output step type:
    elif step_type in ("input", "output"):
        properties["content"] =  properties.get("content", "")
        properties["has_files"] = str(properties.get("has_files")).lower().strip()
    # Changes specific to action/custom step type:
    elif step_type in ("action", "custom"):
        properties["has_files"] = str(properties.get("has_files")).lower().strip()
    # Changes specific to storage step type:
    elif step_type == "storage":
        properties["endpoint"] = properties.get("endpoint", "")
        # Database (lowercase)
        db = str(properties.get("database", "minio")).lower().strip()
        if db not in ("minio", "sqlite", "chromadb"):
            db = "minio"
        properties["database"] = db
    # Changes specific to api step type:
    elif step_type == "api":
        properties["endpoint"] = properties.get("endpoint", "")
    # Ensure we never attempt to store maps / File objects
    properties.pop("files", None)
    # ---- Cypher update ----
    # We match by flow_id (since you send flow_id from the canvas)
    query = """
    MATCH (n:STEP {flow_id: $flow_id})
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(n)
    SET n += $props
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
            return jsonify(record["n"]._properties), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500
    
# Deletes all nodes and edges, returns STEP flow_ids deleted
@app.route('/neo4j_clear_nodes', methods=['DELETE', 'OPTIONS'])
@require_auth
def neo4j_clear_nodes():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    print("[neo4j_api.py] Clearing all nodes from Neo4j")
    query_get = """
    MATCH (s:STEP)
    WHERE s.flow_id IS NOT NULL
    RETURN collect(toString(s.flow_id)) AS flow_ids
    """
    query_delete = """
    MATCH (n)
    DETACH DELETE n
    """
    try:
        with driver.session() as session:
            record = session.run(query_get).single()
            flow_ids = record["flow_ids"] if record and record["flow_ids"] else []
            session.run(query_delete)
        return jsonify({
            "status": "ok",
            "message": "All nodes deleted",
            "deleted_step_flow_ids": flow_ids
        }), 200
    except Exception as e:
        print("[neo4j_api.py] Error clearing Neo4j:", e)
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
                session.run("MATCH (n) DETACH DELETE n")
            elif step_count > 1:
                session.run(
                    "MATCH (s:STEP {flow_id: $flow_id}) DETACH DELETE s",
                    flow_id=flow_id,
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
    if not from_flow_id or not to_flow_id:
        return jsonify({"error": "Missing from_flow_id or to_flow_id"}), 400
    if str(from_flow_id) == str(to_flow_id):
        return jsonify({"error": "Cannot relate a node to itself"}), 400
    query = """
    MATCH (prev:STEP {flow_id: $from_flow_id})
    MATCH (next:STEP {flow_id: $to_flow_id})
    MERGE (prev)-[:FLOWS_TO]->(next)
    WITH prev, next
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(prev)
    SET p.updated_at = datetime()
    RETURN prev.flow_id AS from_flow_id, next.flow_id AS to_flow_id
    """
    try:
        with driver.session() as session:
            record = session.run(query, {
                "from_flow_id": str(from_flow_id),
                "to_flow_id": str(to_flow_id)
            }).single()
            if not record:
                return jsonify({"error": "STEP node(s) not found for given flow_id(s)"}), 404
            return jsonify({
                "from_flow_id": record["from_flow_id"],
                "to_flow_id": record["to_flow_id"],
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
    if not from_flow_id or not to_flow_id:
        return jsonify({"error": "Missing from_flow_id or to_flow_id"}), 400
    query = """
    MATCH (prev:STEP {flow_id: $from_flow_id})
    MATCH (next:STEP {flow_id: $to_flow_id})
    OPTIONAL MATCH (prev)-[r]->(next)
    DELETE r
    WITH prev, next
    OPTIONAL MATCH (p:PIPELINE)-[:HAS_STEP]->(prev)
    SET p.updated_at = datetime()
    RETURN prev.flow_id AS from_flow_id, next.flow_id AS to_flow_id
    """
    try:
        with driver.session() as session:
            record = session.run(query, {
                "from_flow_id": str(from_flow_id),
                "to_flow_id": str(to_flow_id)
            }).single()
            if not record:
                return jsonify({"error": "STEP node(s) not found for given flow_id(s)"}), 404
            return jsonify({
                "from_flow_id": record["from_flow_id"],
                "to_flow_id": record["to_flow_id"],
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
    RETURN f.filename AS filename, f.bucket AS bucket
    """
    try:
        with driver.session() as session:
            result = session.run(query)
            files = [
                {
                    "filename": record["filename"],
                    "bucket": record["bucket"]
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
    MATCH (p:PIPELINE)
    RETURN 
      p.version AS version, 
      toString(p.updated_at) AS updated_at, 
      toString(p.created_at) AS created_at
    """
    try:
        with driver.session() as session:
            if not _label_exists(session, "PIPELINE"):
                return jsonify({
                    "version": None,
                    "created_at": None,
                    "updated_at": None
                }), 200
            result = session.run(query)
            record = result.single()
            if record is None:
                return jsonify({
                    "version": None,
                    "created_at": None,
                    "updated_at": None
                }), 200
            return jsonify({
                "version": record["version"],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"]
            }), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500
    
@app.route('/neo4j_get_pipeline_updated_at', methods=['GET'])
@require_auth
def neo4j_get_pipeline_updated_at():
    print("[neo4j_api.py] Received request to get PIPELINE.updated_at")
    query = """
    MATCH (p:PIPELINE)
    RETURN toString(p.updated_at) AS updated_at
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

# (Internal) Run query by LLM
@app.route('/neo4j_run_query', methods=['POST'])
@require_auth
def neo4j_run_query():
    data = request.json
    query = data['query']
    print("[neo4j_api.py] Received query to execute in Neo4J:", query)
    with driver.session() as session:
        session_result = session.run(query)
        # We are assuming that the query returns something to jsonify
        results = [record.data() for record in session_result]
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
    MATCH (p:PIPELINE)
    SET p.updated_at = datetime()
    RETURN s.flow_id AS flow_id
    """
    try:
        with driver.session() as session:
            session.run(query, flow_id=flow_id, x=x, y=y)
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/neo4j_get_graph', methods=['GET'])
@require_auth
def neo4j_get_graph():
    print("[neo4j_api.py] Received request to get graph (ReactFlow export-like).")
    query = """
    MATCH (p:PIPELINE {status:'design'})
    OPTIONAL MATCH (p)-[:HAS_STEP]->(s:STEP)
    OPTIONAL MATCH (s)-[:FLOWS_TO]->(t:STEP)
    OPTIONAL MATCH (s)-[:HAS_FILE]->(f:FILE)
    WITH
      p,
      s,
      t,
      collect(DISTINCT f { .filename, .bucket, added_at: toString(f.added_at) }) AS files_for_step
    RETURN
      toString(p.updated_at) AS updated_at,
      collect(DISTINCT {
        step: s,
        files: files_for_step
      }) AS step_rows,
      collect(DISTINCT {
        source: s.flow_id,
        target: t.flow_id
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

                step_kind = str(props.get("type") or "custom")

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

                    # add files so polling doesn't wipe them
                    "files": filenames,

                    # optional richer info (bucket + added_at)
                    "file_buckets": files_for_step,
                }

                if "content" in props:
                    data["content"] = props.get("content") or ""
                if "has_files" in props:
                    data["has_files"] = props.get("has_files")
                if "endpoint" in props:
                    data["endpoint"] = props.get("endpoint")
                if "database" in props:
                    data["database"] = props.get("database")
                if "param_json" in props:
                    data["param_json"] = props.get("param_json")

                nodes.append({
                    "id": node_id,
                    "type": "custom",
                    "position": {"x": x, "y": y},
                    "data": data,
                })

            edges = []
            for f in flows:
                src = f.get("source") if isinstance(f, dict) else None
                tgt = f.get("target") if isinstance(f, dict) else None
                if src is None or tgt is None:
                    continue
                src = str(src)
                tgt = str(tgt)
                edges.append({
                    "id": f"reactflow__edge-{src}-{tgt}",
                    "source": src,
                    "target": tgt,
                    "sourceHandle": None,
                    "targetHandle": None,
                })

            return jsonify({
                "updated_at": updated_at,
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


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5001)
