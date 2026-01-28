from flask import Flask, request, jsonify
from neo4j import GraphDatabase
import uuid
import configparser
import json
import os


config_neo4j = configparser.ConfigParser()
config_neo4j.read('neo4j_config.ini')

# Global graph data
graph_data = []

app = Flask(__name__)

driver = GraphDatabase.driver(config_neo4j.get('neo4j','uri'), auth=(config_neo4j.get('neo4j','username'), config_neo4j.get('neo4j','password')))

# Define a function to set the CORS headers
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = 'http://localhost:8080'  # allowed origin
    response.headers['Access-Control-Allow-Methods'] = 'OPTIONS, GET, POST'  # Adjust as needed
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# Apply the CORS function to all routes using the after_request decorator
@app.after_request
def apply_cors(response):
    return add_cors_headers(response)

# Adds a pipeline step into Neo4J:
@app.route('/neo4j_add_node', methods=['POST'])
def neo4j_add_node():
    print("[neo4j_api.py] Received query to add STEP node in Neo4j.")
    data = request.json
    properties = data.get("properties", {})
    step_type = str(properties.get("type")).lower()
    # Set default properties:
    properties.setdefault("type", step_type)
    properties.setdefault("label", properties.get("label", ""))
    properties.setdefault("description", properties.get("description", ""))
    properties.setdefault("flow_id", properties.get("flow_id")) # Not necessary for now
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
    query = f"""
        CREATE (n:STEP)
        SET n += $props
        SET n.uid = randomUUID()
        RETURN n
    """
    try:
        with driver.session() as session:
            result = session.run(query, {"props": properties})
            record = result.single()
            return jsonify(record["n"]._properties), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500
    

# Updates a pipeline step in Neo4J:
@app.route('/neo4j_update_node', methods=['POST'])
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
        SET n += $props
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


# (Internal) Run query by LLM
@app.route('/neo4j_run_query', methods=['POST'])
def neo4j_run_query():
    data = request.json
    query = data['query']
    print("[neo4j_api.py] Received query to execute in Neo4J:", query)
    with driver.session() as session:
        session_result = session.run(query)
        # We are assuming that the query returns something to jsonify
        results = [record.data() for record in session_result]
        return jsonify(results)
    
@app.route('/neo4j_clear_all', methods=['DELETE'])
def neo4j_clear_all():
    # Construct the Cypher query with safe string formatting
    query = f"""
        MATCH (n) DETACH DELETE n
    """
    print("[neo4j_api.py] Received query to execute in Neo4j:\n", query)
    try:
        with driver.session() as session:
            session.run(query)
        return jsonify({"status": "Graph cleared."}), 200
    except Exception as e:
        print("[neo4j_api.py] Error executing Neo4j query:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/neo4j_save_graph', methods=['GET'])
def neo4j_save_graph():
    query = """
    MATCH (n:STEP)-[r]->(m:STEP)
    RETURN n, r, m
    UNION
    MATCH (n:STEP)
    WHERE NOT (n)--()
    RETURN n, null AS r, null AS m
    """
    print("[neo4j_api.py] Received query to execute in Neo4j:\n", query)
    try:
        with driver.session() as session:
            results = session.run(query)
            nodes = {}
            relationships = []
            for record in results:
                n = record['n']
                n_id = str(n.element_id)
                if n_id not in nodes:
                    nodes[n_id] = {
                        "id": n_id,
                        "labels": list(n.labels),
                        "properties": dict(n.items()),
                    }
                if record['m']:
                    m = record['m']
                    m_id = str(m.element_id)
                    if m_id not in nodes:
                        nodes[m_id] = {
                            "id": m_id,
                            "labels": list(m.labels),
                            "properties": dict(m.items()),
                        }
                r = record.get('r')
                if r is not None:
                    relationships.append({
                        "id": str(r.element_id),
                        "type": r.type,
                        "startNode": str(r.start_node.element_id),
                        "endNode": str(r.end_node.element_id),
                        "properties": dict(r.items()),  # or r._properties if needed
                    })
            graph = {
                "nodes": list(nodes.values()),
                "relationships": relationships,
            }
            # Uncomment for download within folder: 
            # Ensure the downloads directory exists
            # os.makedirs("downloads", exist_ok=True)
            # Write the file to local downloads folder:
            # with open("downloads/graph.json", "w") as f:
            #    json.dump(graph, f, indent=2)
            #print("[neo4j_api.py] Graph saved to downloads/graph.json")
            # return jsonify({"status": "Graph saved."}), 200
            return jsonify(graph), 200
    except Exception as e:
        print("[neo4j_api.py] Error under graph save:", e)
        return jsonify({"error": str(e)}), 500
    
@app.route('/neo4j_load_graph', methods=['POST'])
def neo4j_load_graph():
    data = request.get_json()
    nodes = data.get("nodes", [])
    relationships = data.get("relationships", [])
    try:
        with driver.session() as session:
            # 1. Clear current graph
            session.run("MATCH (n) DETACH DELETE n")
            print("[neo4j_api.py] Deleted existing graph.")
            # 2. Create Step nodes and build ID → UID map
            id_to_uid = {}
            for node in nodes:
                labels = ":".join(node.get("labels", [])) or "Node"
                props = node.get("properties", {})
                node_id = node.get("id")
                uid = props.get("uid")
                description = props.get("description")
                user_label = props.get("user_label")
                
                if uid and node_id:
                    id_to_uid[node_id] = uid

                query = f"CREATE (n:{labels}) SET n = $props"
                session.run(query, {"props": props})
            
            # 3. Create relationships using uid from mapped ids
            for rel in relationships:
                rel_type = rel.get("type", "")
                props = rel.get("properties", {})

                start_id = rel.get("startNode")
                end_id = rel.get("endNode")
                start_uid = id_to_uid.get(str(start_id))
                end_uid = id_to_uid.get(str(end_id))

                if start_uid and end_uid:
                    query = f"""
                    MATCH (a:STEP {{uid: $start_uid}})
                    MATCH (b:STEP {{uid: $end_uid}})
                    CREATE (a)-[r:{rel_type}]->(b)
                    SET r = $props
                    """
                    session.run(query, {
                        "start_uid": start_uid,
                        "end_uid": end_uid,
                        "props": props
                    })
            # 4. Collect all buckets - UIDs used as bucket names in MinIO
            bucket_query = """
                MATCH (fn:STEP)
                RETURN fn.uid AS uid
            """
            result = session.run(bucket_query)
            buckets = [record["uid"] for record in result]
            print(f"[neo4j_api.py] Loaded graph and collected {len(buckets)} bucket uids for storage.")
            return jsonify({"buckets": buckets}), 200
    except Exception as e:
        print("[neo4j_api.py] Error loading graph:", e)
        return jsonify({"error": str(e)}), 500

    
@app.route('/api/neo4j_get_graph', methods=['GET'])
def neo4j_get_graph():
    # API wrap for function obtaining current graph in Neo4J
    data = neo4j_graph() 
    return jsonify(data)

def neo4j_graph():
    query = f"""
    MATCH (n:STEP)-[r]->(m:STEP)
    RETURN n, r, m
    """
    try:
        with driver.session() as session:
            results = session.run(query)
            new_graph_data = []
            for record in results:
                node1 = record["n"]
                rel = record["r"]
                node2 = record["m"]
                # Adjusted to include relationship details as specified
                new_graph_data.append({
                    "n": {
                        "identity": int(node1.element_id),
                        "labels": list(node1.labels),
                        "properties": dict(node1),
                        "elementId": str(node1.element_id)
                    },
                    "r": {
                        "identity": int(rel.element_id),
                        "start": int(rel.start_node.element_id),
                        "end": int(rel.end_node.element_id),
                        "type": rel.type,
                        "properties": dict(rel),
                        "elementId": str(rel.element_id),
                        "startNodeElementId": str(rel.start_node.element_id),
                        "endNodeElementId": str(rel.end_node.element_id)
                    },
                    "m": {
                        "identity": node2.element_id,
                        "labels": list(node2.labels),
                        "properties": dict(node2),
                        "elementId": str(node2.element_id)
                    },
                })
            return new_graph_data
    except Exception as e:
        print(f"An error occurred: {e}")
        return []



@app.route('/neo4j_delete_node/<uid>', methods=['DELETE'])
def delete_node(uid):
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