import asyncio
import json

import requests


def _auth_headers(authorization: str | None = None) -> dict:
    headers = {}
    if authorization:
        headers["Authorization"] = authorization
    return headers


def _json_headers(authorization: str | None = None) -> dict:
    headers = _auth_headers(authorization)
    headers["Content-Type"] = "application/json"
    return headers


async def fetch_pipeline_graph(
    neo4j_api_base_url: str,
    authorization: str | None = None,
) -> dict:
    """Fetch the current pipeline nodes, files and flows from Neo4j."""
    api_url = f"{neo4j_api_base_url}/neo4j_get_graph"
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.get(
                api_url,
                timeout=60,
                headers=_auth_headers(authorization),
            ),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load pipeline graph from Neo4j ({api_url}): {exc}"
        ) from exc


async def fetch_pipeline_versions(
    neo4j_api_base_url: str,
    include_graph: bool = False,
    authorization: str | None = None,
) -> list[dict]:
    """Fetch available pipeline versions, optionally including each saved graph."""
    api_url = f"{neo4j_api_base_url}/neo4j_list_pipeline_versions"
    params = {"include_graph": "true"} if include_graph else None
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.get(
                api_url,
                params=params,
                timeout=60,
                headers=_auth_headers(authorization),
            ),
        )
        response.raise_for_status()
        payload = response.json()
        versions = payload.get("versions") if isinstance(payload, dict) else []
        return versions if isinstance(versions, list) else []
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load pipeline versions from Neo4j ({api_url}): {exc}"
        ) from exc


async def sync_backend_to_canvas_graph(
    neo4j_api_base_url: str,
    graph: dict,
    active_version_uid: str | None = None,
    active_version_name: str | None = None,
    authorization: str | None = None,
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
                headers=_json_headers(authorization),
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
    authorization: str | None = None,
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
                headers=_json_headers(authorization),
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
    authorization: str | None = None,
) -> str:
    """Run a Cypher query through the Neo4j API and return a string payload."""
    try:
        print("[graph_client.py] Executing Neo4J query of type: " + query_type)
        api_url = f"{neo4j_api_base_url}/neo4j_run_query"
        payload = {"query": query}
        headers = _json_headers(authorization)
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
