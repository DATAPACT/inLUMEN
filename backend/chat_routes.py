from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from auth_middleware import require_auth
from chat_state import clear_state_from_disk
from conversations import ConversationError, chat_scope, conversation_store


def create_chat_blueprint():
    blueprint = Blueprint("chat_history", __name__)

    @blueprint.errorhandler(ConversationError)
    def conflict(error):
        return jsonify({"error": str(error), "code": error.code}), error.status

    @blueprint.errorhandler(SQLAlchemyError)
    def unavailable(_error):
        return jsonify({"error": "Conversation history is temporarily unavailable."}), 503

    @blueprint.after_request
    def private(response):
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @blueprint.route("/api/chat/conversation", methods=["GET", "DELETE", "OPTIONS"])
    @require_auth
    def active_conversation():
        if request.method == "OPTIONS":
            return "", 200
        store, scope = conversation_store(), chat_scope()
        if request.method == "DELETE":
            payload = request.get_json(silent=True) or {}
            cid = store.reset(scope, payload.get("conversation_id"))
            if cid:
                clear_state_from_disk(cid)
            return jsonify({"ok": True, "conversation_id": cid}), 200
        try:
            after = int(request.args.get("after", 0))
            before = int(request.args["before"]) if "before" in request.args else None
            limit = int(request.args.get("limit", 100))
            if not 0 <= after <= 2**53 or before is not None and not 1 <= before <= 2**53 or not 1 <= limit <= 100:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid history cursor or page size."}), 400
        return jsonify(store.read(scope, after=after, before=before, limit=limit,
                                  conversation_id=request.args.get("conversation_id"))), 200

    @blueprint.route("/api/chat/messages/<message_id>", methods=["PATCH", "OPTIONS"])
    @require_auth
    def update_message(message_id):
        if request.method == "OPTIONS":
            return "", 200
        payload = request.get_json(silent=True) or {}
        conversation_store().update_proposal(chat_scope(), message_id, payload.get("proposal_status"))
        return jsonify({"ok": True}), 200

    return blueprint
