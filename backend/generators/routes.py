from __future__ import annotations

from io import BytesIO
from typing import Callable

from flask import Blueprint, jsonify, make_response, request

from async_runtime import run_async
from auth_middleware import require_auth
from deployment_artifacts import extract_pipeline_steps
from graph_client import dispatch_graph_request, fetch_pipeline_graph
from minio_gateway import get_minio_client

from .base import GeneratedRuntimeArtifacts
from .registry import generate_runtime_artifacts


GraphLoader = Callable[[], dict]
ArtifactPersister = Callable[[GeneratedRuntimeArtifacts], dict]


def _authorization_headers() -> dict[str, str]:
    authorization = request.headers.get("Authorization")
    return {"Authorization": authorization} if authorization else {}


def _load_graph() -> dict:
    return run_async(
        fetch_pipeline_graph(
            authorization=request.headers.get("Authorization"),
        )
    )


def _persist_artifacts(bundle: GeneratedRuntimeArtifacts) -> dict:
    client = get_minio_client()
    bucket = f"files-step-id-{bundle.flow_id}".lower()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)

    headers = _authorization_headers()
    stored_files = []
    for generated_file in bundle.files:
        encoded = generated_file.content.encode("utf-8")
        client.put_object(
            bucket,
            generated_file.filename,
            BytesIO(encoded),
            length=len(encoded),
            content_type=generated_file.content_type,
        )
        graph_response = dispatch_graph_request(
            "neo4j_add_file",
            method="POST",
            json_payload={
                "properties": {
                    "flow_id": bundle.flow_id,
                    "filename": generated_file.filename,
                }
            },
            headers=headers,
        )
        graph_response.raise_for_status()
        stored_files.append(
            {
                "filename": generated_file.filename,
                "bucket": bucket,
                "content_type": generated_file.content_type,
            }
        )

    artifact_record = bundle.to_dict(include_content=False)
    artifact_record["files"] = stored_files
    artifact_record["status"] = "current"
    update_response = dispatch_graph_request(
        "neo4j_update_generated_artifact",
        method="POST",
        json_payload={
            "flow_id": bundle.flow_id,
            "generated_artifact": artifact_record,
        },
        headers=headers,
    )
    update_response.raise_for_status()
    return artifact_record


def create_generator_blueprint(
    *,
    graph_loader: GraphLoader | None = None,
    artifact_persister: ArtifactPersister | None = None,
) -> Blueprint:
    generators = Blueprint("generators", __name__)
    load_graph = graph_loader or _load_graph
    persist_artifacts = artifact_persister or _persist_artifacts

    @generators.route(
        "/api/nodes/<flow_id>/generate",
        methods=["POST", "OPTIONS"],
    )
    @require_auth
    def generate_node_artifacts(flow_id: str):
        if request.method == "OPTIONS":
            return make_response("", 200)

        graph = load_graph()
        steps = extract_pipeline_steps(graph)
        step = next(
            (candidate for candidate in steps if candidate["flow_id"] == flow_id),
            None,
        )
        if step is None:
            return jsonify({"error": f"Node {flow_id!r} was not found."}), 404

        try:
            bundle = generate_runtime_artifacts(step, graph)
            payload = bundle.to_dict(include_content=True)
            request_payload = request.get_json(silent=True) or {}
            should_persist = not (
                isinstance(request_payload, dict)
                and request_payload.get("persist") is False
            )
            if should_persist:
                payload["generated_artifact"] = persist_artifacts(bundle)
            else:
                payload["generated_artifact"] = {
                    **bundle.to_dict(include_content=False),
                    "status": "current",
                }
            return jsonify(payload), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422
        except Exception as exc:
            return jsonify({"error": f"Artifact generation failed: {exc}"}), 500

    return generators
