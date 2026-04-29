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
