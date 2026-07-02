from __future__ import annotations

from flask import Blueprint, jsonify, make_response, request

from auth_middleware import require_auth

from .registry import NodeDefinitionRegistry, get_node_definition_registry


def create_node_definitions_blueprint(
    registry: NodeDefinitionRegistry | None = None,
) -> Blueprint:
    node_definitions = Blueprint("node_definitions", __name__)

    @node_definitions.route("/api/node-definitions", methods=["GET", "OPTIONS"])
    @require_auth
    def list_node_definitions():
        if request.method == "OPTIONS":
            return make_response("", 200)
        active_registry = registry or get_node_definition_registry()
        include_disabled = str(request.args.get("include_disabled") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        return jsonify(
            active_registry.response_payload(include_disabled=include_disabled)
        ), 200

    return node_definitions
