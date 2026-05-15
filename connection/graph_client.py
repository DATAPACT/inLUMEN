import asyncio
import json

import requests


async def fetch_pipeline_graph(neo4j_api_base_url: str) -> dict:
    """Fetch the current pipeline nodes, files and flows from Neo4j."""
    api_url = f"{neo4j_api_base_url}/neo4j_get_graph"
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


async def sync_backend_to_canvas_graph(
    neo4j_api_base_url: str,
    graph: dict,
    active_version_uid: str | None = None,
    active_version_name: str | None = None,
) -> dict:
    """Make Neo4j match the visible canvas graph before an agent turn."""
    api_url = f"{neo4j_api_base_url}/neo4j_sync_graph"
    payload = {"graph": graph}
    if active_version_uid:
        payload["active_version_uid"] = active_version_uid
    if active_version_name:
        payload["version_name"] = active_version_name
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                api_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=60,
            ),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to sync visible canvas graph to Neo4j ({api_url}): {exc}"
        ) from exc


async def save_active_pipeline_version(
    neo4j_api_base_url: str,
    graph: dict,
    active_version_uid: str,
    active_version_name: str,
) -> dict:
    """Persist the current live graph into the requested active pipeline version."""
    api_url = f"{neo4j_api_base_url}/neo4j_save_pipeline_active_version"
    payload = {
        "graph": graph,
        "uid": active_version_uid,
        "name": active_version_name,
    }
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                api_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=60,
            ),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to save active pipeline version ({api_url}): {exc}"
        ) from exc


async def run_neo4j_query(
    neo4j_api_base_url: str,
    query: str,
    query_type: str,
) -> str:
    """Run a Cypher query through the Neo4j API and return a string payload."""
    try:
        print("[graph_client.py] Executing Neo4J query of type: " + query_type)
        api_url = f"{neo4j_api_base_url}/neo4j_run_query"
        payload = {"query": query}
        headers = {"Content-Type": "application/json"}
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(api_url, data=json.dumps(payload), headers=headers),
        )
        if response.status_code == 200:
            return repr(response.text)
        return repr({"Error": f"{response.status_code} - {response.text}"})
    except Exception as exc:
        return repr({"error": str(exc)})
