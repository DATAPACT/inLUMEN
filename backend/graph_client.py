import asyncio

from local_api_client import LocalApiResponse, dispatch_flask_request


def _auth_headers(authorization: str | None = None) -> dict:
    headers = {}
    if authorization:
        headers["Authorization"] = authorization
    return headers


def _json_headers(authorization: str | None = None) -> dict:
    headers = _auth_headers(authorization)
    headers["Content-Type"] = "application/json"
    return headers


def dispatch_graph_request(
    backend_path: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    data=None,
    json_payload=None,
    files=None,
    form: dict | None = None,
    headers: dict | None = None,
) -> LocalApiResponse:
    return dispatch_flask_request(
        _neo4j_app(),
        backend_path,
        method=method,
        params=params,
        data=data,
        json_payload=json_payload,
        files=files,
        form=form,
        headers=headers,
    )


def _neo4j_app():
    from neo4j_api import app

    return app


def check_graph_health() -> bool:
    return dispatch_graph_request("health").ok


def update_pipeline_overview(
    _graph_backend: str | None,
    payload: dict,
    authorization: str | None = None,
) -> LocalApiResponse:
    return dispatch_graph_request(
        "neo4j_update_pipeline_overview",
        method="POST",
        json_payload=payload,
        headers=_json_headers(authorization),
    )


async def fetch_pipeline_graph(
    _graph_backend: str | None = None,
    authorization: str | None = None,
) -> dict:
    """Fetch the current pipeline nodes, files and flows from Neo4j."""
    api_name = "in-process Neo4j adapter"
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: dispatch_graph_request(
                "neo4j_get_graph",
                headers=_auth_headers(authorization),
            ),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load pipeline graph from Neo4j ({api_name}): {exc}"
        ) from exc


async def fetch_pipeline_versions(
    _graph_backend: str | None = None,
    include_graph: bool = False,
    authorization: str | None = None,
) -> list[dict]:
    """Fetch available pipeline versions, optionally including each saved graph."""
    params = {"include_graph": "true"} if include_graph else None
    api_name = "in-process Neo4j adapter"
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: dispatch_graph_request(
                "neo4j_list_pipeline_versions",
                params=params,
                headers=_auth_headers(authorization),
            ),
        )
        response.raise_for_status()
        payload = response.json()
        versions = payload.get("versions") if isinstance(payload, dict) else []
        return versions if isinstance(versions, list) else []
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load pipeline versions from Neo4j ({api_name}): {exc}"
        ) from exc


async def sync_backend_to_canvas_graph(
    graph: dict,
    active_version_uid: str | None = None,
    active_version_name: str | None = None,
    authorization: str | None = None,
) -> dict:
    """Make Neo4j match the visible canvas graph before an agent turn."""
    payload = {"graph": graph}
    if active_version_uid:
        payload["active_version_uid"] = active_version_uid
    if active_version_name:
        payload["version_name"] = active_version_name
    api_name = "in-process Neo4j adapter"
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: dispatch_graph_request(
                "neo4j_sync_graph",
                method="POST",
                json_payload=payload,
                headers=_json_headers(authorization),
            ),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to sync visible canvas graph to Neo4j ({api_name}): {exc}"
        ) from exc


async def save_active_pipeline_version(
    graph: dict,
    active_version_uid: str,
    active_version_name: str,
    authorization: str | None = None,
) -> dict:
    """Persist the current live graph into the requested active pipeline version."""
    payload = {
        "graph": graph,
        "uid": active_version_uid,
        "name": active_version_name,
    }
    api_name = "in-process Neo4j adapter"
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: dispatch_graph_request(
                "neo4j_save_pipeline_active_version",
                method="POST",
                json_payload=payload,
                headers=_json_headers(authorization),
            ),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to save active pipeline version ({api_name}): {exc}"
        ) from exc


async def run_neo4j_query(
    query: str,
    query_type: str,
    authorization: str | None = None,
) -> str:
    """Run a Cypher query through the Neo4j API and return a string payload."""
    try:
        print("[graph_client.py] Executing Neo4J query of type: " + query_type)
        payload = {"query": query}
        headers = _json_headers(authorization)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: dispatch_graph_request(
                "neo4j_run_query",
                method="POST",
                json_payload=payload,
                headers=headers,
            ),
        )
        if response.status_code == 200:
            return repr(response.text)
        return repr({"Error": f"{response.status_code} - {response.text}"})
    except Exception as exc:
        return repr({"error": str(exc)})
