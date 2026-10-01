"""Durable, private chat transcripts and idempotent pipeline-editor turns.

Agent checkpoints stay in chat_state; this store contains only user-visible
messages and turn receipts. PostgreSQL transactions coordinate Gunicorn workers.
SQLite is the durable development fallback and the isolated test implementation.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from auth_middleware import current_principal, is_auth_enabled


class ConversationError(Exception):
    def __init__(self, message: str, status: int = 409, code: str = "conversation_conflict"):
        super().__init__(message)
        self.status, self.code = status, code


def _now():
    return datetime.now(timezone.utc).isoformat()


def chat_scope():
    principal = current_principal()
    return {"workspace_id": principal.workspace_id, "user_id": principal.user_id}


def registry_turn_id(scope, turn_id):
    return hashlib.sha256(f"{scope['workspace_id']}:{scope['user_id']}:{turn_id}".encode()).hexdigest()


class ConversationStore:
    def __init__(self, location: str):
        if location == ":memory:":
            url, options = "sqlite+pysqlite:///:memory:", {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        elif "://" in location:
            url, options = location.replace("postgresql://", "postgresql+psycopg://", 1), {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5}
        else:
            path = Path(location).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            url, options = f"sqlite+pysqlite:///{path}", {"connect_args": {"timeout": 30, "check_same_thread": False}}
        self.engine = create_engine(url, **options)
        self.lock = threading.RLock()
        self.postgres = self.engine.dialect.name == "postgresql"
        if not self.postgres:
            @event.listens_for(self.engine, "connect")
            def sqlite_settings(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
        identity = "UUID" if self.postgres else "TEXT"
        owner_fk = "REFERENCES app_users(id) ON DELETE CASCADE" if self.postgres else ""
        workspace_fk = "REFERENCES workspaces(id) ON DELETE CASCADE" if self.postgres else ""
        statements = [
            f"""CREATE TABLE IF NOT EXISTS chat_conversations (
                id TEXT PRIMARY KEY, workspace_id {identity} NOT NULL {workspace_fk},
                user_id {identity} NOT NULL {owner_fk}, revision BIGINT NOT NULL DEFAULT 0,
                reset_revision BIGINT NOT NULL DEFAULT 0, sequence BIGINT NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(id,workspace_id,user_id))""",
            f"""CREATE TABLE IF NOT EXISTS chat_active_conversations (
                workspace_id {identity} NOT NULL, user_id {identity} NOT NULL, conversation_id TEXT NOT NULL,
                PRIMARY KEY(workspace_id,user_id),
                FOREIGN KEY(conversation_id,workspace_id,user_id)
                    REFERENCES chat_conversations(id,workspace_id,user_id) ON DELETE CASCADE)""",
            f"""CREATE TABLE IF NOT EXISTS chat_turns (
                workspace_id {identity} NOT NULL, user_id {identity} NOT NULL, turn_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','cancelled')),
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                response_json TEXT, response_status INTEGER, heartbeat_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id,user_id,turn_id),
                FOREIGN KEY(conversation_id,workspace_id,user_id)
                    REFERENCES chat_conversations(id,workspace_id,user_id) ON DELETE CASCADE)""",
            """CREATE INDEX IF NOT EXISTS chat_turns_pending_idx ON chat_turns(workspace_id,status,heartbeat_at)""",
            """CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL, status TEXT NOT NULL,
                proposal_status TEXT, sequence BIGINT NOT NULL, revision BIGINT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(conversation_id,sequence), UNIQUE(conversation_id,turn_id,role))""",
            """CREATE INDEX IF NOT EXISTS chat_messages_revision_idx ON chat_messages(conversation_id,revision)""",
            f"""CREATE TABLE IF NOT EXISTS chat_cancel_requests (
                workspace_id {identity} NOT NULL {workspace_fk}, user_id {identity} NOT NULL {owner_fk},
                turn_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(workspace_id,user_id,turn_id))""",
        ]
        with self.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    @contextmanager
    def transaction(self, scope):
        with self.lock, self.engine.begin() as connection:
            if self.postgres:
                # Serialize editors in a shared workspace as well as two tabs
                # creating the same owner's active conversation simultaneously.
                connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                   {"key": "chat:" + scope["workspace_id"]})
            else:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def active(connection, scope):
        return connection.execute(text("""SELECT c.* FROM chat_conversations c
            JOIN chat_active_conversations a ON c.id=a.conversation_id
            WHERE a.workspace_id=:workspace_id AND a.user_id=:user_id"""), scope).mappings().first()

    @staticmethod
    def _increment(connection, conversation_id, *, message=False):
        return connection.execute(text("""UPDATE chat_conversations SET revision=revision+1,
            sequence=sequence+:increment, updated_at=:now WHERE id=:id RETURNING revision,sequence"""),
            {"id": conversation_id, "increment": int(message), "now": _now()}).mappings().one()

    def _message(self, connection, conversation_id, turn_id, role, content, status, proposal_status=None):
        counter = self._increment(connection, conversation_id, message=True)
        message_id = str(uuid.uuid4())
        connection.execute(text("""INSERT INTO chat_messages
            (id,conversation_id,turn_id,role,content,status,proposal_status,sequence,revision,created_at)
            VALUES(:id,:conversation,:turn,:role,:content,:status,:proposal,:sequence,:revision,:now)"""),
            {"id": message_id, "conversation": conversation_id, "turn": turn_id, "role": role,
             "content": content, "status": status, "proposal": proposal_status,
             "sequence": counter["sequence"], "revision": counter["revision"], "now": _now()})
        return message_id

    def _expire(self, connection, scope, *, workspace=False):
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        query = """SELECT * FROM chat_turns WHERE workspace_id=:workspace_id
            AND status='pending' AND heartbeat_at<:cutoff"""
        if not workspace:
            query += " AND user_id=:user_id"
        rows = connection.execute(text(query + (" FOR UPDATE" if self.postgres else "")), {**scope, "cutoff": cutoff}).mappings().all()
        for row in rows:
            response = {"session_id": row["conversation_id"], "turn_id": row["turn_id"],
                        "error": "The request was interrupted. Check the saved pipeline before trying again."}
            self._finish(connection, {"workspace_id": str(row["workspace_id"]), "user_id": str(row["user_id"])}, row["turn_id"], response, 500, "failed")

    def read(self, scope, *, after=0, before=None, limit=100, conversation_id=None):
        limit = min(max(limit, 1), 100)
        # Read-only, short queries; no workspace advisory lock on every poll.
        with self.lock, self.engine.begin() as connection:
            self._expire(connection, scope)
            conversation = self.active(connection, scope)
            if not conversation:
                return {"conversation": None, "messages": [], "reset": True, "has_older": False, "has_more": False}
            cid = conversation["id"]
            reset = after == 0 or (conversation_id and conversation_id != cid) or after < conversation["reset_revision"] or after > conversation["revision"]
            params = {"cid": cid, "limit": limit + 1, "after": after, "before": before}
            if before is not None:
                query = "SELECT * FROM chat_messages WHERE conversation_id=:cid AND sequence<:before ORDER BY sequence DESC LIMIT :limit"
            elif reset:
                query = "SELECT * FROM chat_messages WHERE conversation_id=:cid ORDER BY sequence DESC LIMIT :limit"
            else:
                query = "SELECT * FROM chat_messages WHERE conversation_id=:cid AND revision>:after ORDER BY revision LIMIT :limit"
            messages = [dict(row) for row in connection.execute(text(query), params).mappings()]
            more = len(messages) > limit
            messages = messages[:limit]
            if reset or before is not None:
                messages.reverse()
            pending = connection.execute(text("""SELECT turn_id,cancel_requested FROM chat_turns
                WHERE conversation_id=:cid AND status='pending'"""), {"cid": cid}).mappings().first()
            cursor = max((message["revision"] for message in messages), default=after) if more and not reset and before is None else conversation["revision"]
            return {"conversation": {"id": cid, "revision": cursor, "updated_at": conversation["updated_at"],
                    "pending_turn_id": pending["turn_id"] if pending else None,
                    "cancelling": bool(pending and pending["cancel_requested"])},
                    "messages": messages, "reset": reset and before is None,
                    "has_older": more if reset or before is not None else False,
                    "has_more": more and not reset and before is None}

    def start(self, scope, *, turn_id, request_sha256, user_message, conversation_id=None):
        with self.transaction(scope) as connection:
            self._expire(connection, scope, workspace=True)
            existing = connection.execute(text("""SELECT * FROM chat_turns WHERE workspace_id=:workspace_id
                AND user_id=:user_id AND turn_id=:turn_id"""), {**scope, "turn_id": turn_id}).mappings().first()
            if existing:
                if existing["request_sha256"] != request_sha256:
                    raise ConversationError("This request identifier was already used for another message.", code="turn_conflict")
                if existing["status"] == "pending":
                    raise ConversationError("This request is already processing. Its result will appear in the conversation.", code="turn_pending")
                return existing["conversation_id"], json.loads(existing["response_json"]), existing["response_status"]
            active = self.active(connection, scope)
            if conversation_id and (not active or active["id"] != conversation_id):
                raise ConversationError("The active conversation changed. Refresh the conversation before sending.", 409, "conversation_changed")
            pending = connection.execute(text("SELECT turn_id FROM chat_turns WHERE workspace_id=:workspace_id AND status='pending' LIMIT 1"), scope).first()
            if pending:
                raise ConversationError("A pipeline edit is already running in this workspace. Wait for it to finish.", code="workspace_chat_busy")
            if not active:
                cid = str(uuid.uuid4())
                connection.execute(text("""INSERT INTO chat_conversations(id,workspace_id,user_id,created_at,updated_at)
                    VALUES(:cid,:workspace_id,:user_id,:now,:now)"""), {**scope, "cid": cid, "now": _now()})
                connection.execute(text("""INSERT INTO chat_active_conversations(workspace_id,user_id,conversation_id)
                    VALUES(:workspace_id,:user_id,:cid)"""), {**scope, "cid": cid})
            else:
                cid = active["id"]
            cancelled = connection.execute(text("""SELECT turn_id FROM chat_cancel_requests WHERE workspace_id=:workspace_id
                AND user_id=:user_id AND turn_id=:turn_id"""), {**scope, "turn_id": turn_id}).first()
            connection.execute(text("""INSERT INTO chat_turns
                (workspace_id,user_id,turn_id,conversation_id,request_sha256,status,cancel_requested,heartbeat_at)
                VALUES(:workspace_id,:user_id,:turn_id,:cid,:sha,'pending',:cancelled,:now)"""),
                {**scope, "turn_id": turn_id, "cid": cid, "sha": request_sha256, "cancelled": int(bool(cancelled)), "now": _now()})
            self._message(connection, cid, turn_id, "user", user_message, "completed")
            return cid, None, None

    def _finish(self, connection, scope, turn_id, response, response_status, status):
        query = """SELECT * FROM chat_turns WHERE workspace_id=:workspace_id
            AND user_id=:user_id AND turn_id=:turn_id AND status='pending'"""
        row = connection.execute(text(query + (" FOR UPDATE" if self.postgres else "")), {**scope, "turn_id": turn_id}).mappings().first()
        if not row:
            return
        # No credentials or internal exception details are copied into messages.
        content = response.get("assistant_message") or "The request failed. Check the saved pipeline before trying again."
        proposal = "pending" if response.get("sync", {}).get("preview_pending") else None
        assistant_id = self._message(connection, row["conversation_id"], turn_id, "assistant", content, status, proposal)
        user_id = connection.execute(text("SELECT id FROM chat_messages WHERE conversation_id=:cid AND turn_id=:turn AND role='user'"),
                                     {"cid": row["conversation_id"], "turn": turn_id}).scalar_one()
        response.update(assistant_message_id=assistant_id, user_message_id=user_id)
        connection.execute(text("""UPDATE chat_turns SET status=:status,response_json=:response,
            response_status=:http,heartbeat_at=:now WHERE workspace_id=:workspace_id AND user_id=:user_id AND turn_id=:turn_id"""),
            {**scope, "turn_id": turn_id, "status": status, "response": json.dumps(response), "http": response_status, "now": _now()})

    def finish(self, scope, turn_id, response, response_status=200, status="completed"):
        with self.transaction(scope) as connection:
            self._finish(connection, scope, turn_id, response, response_status, status)

    def heartbeat(self, scope, turn_id):
        with self.lock, self.engine.begin() as connection:
            row = connection.execute(text("""SELECT status,cancel_requested FROM chat_turns
                WHERE workspace_id=:workspace_id AND user_id=:user_id AND turn_id=:turn_id"""), {**scope, "turn_id": turn_id}).mappings().first()
            if not row or row["status"] != "pending":
                return True
            connection.execute(text("""UPDATE chat_turns SET heartbeat_at=:now WHERE workspace_id=:workspace_id
                AND user_id=:user_id AND turn_id=:turn_id AND status='pending'"""), {**scope, "turn_id": turn_id, "now": _now()})
            return bool(row["cancel_requested"])

    def cancel(self, scope, turn_id):
        with self.transaction(scope) as connection:
            connection.execute(text("""INSERT INTO chat_cancel_requests(workspace_id,user_id,turn_id,created_at)
                VALUES(:workspace_id,:user_id,:turn_id,:now) ON CONFLICT(workspace_id,user_id,turn_id) DO NOTHING"""),
                {**scope, "turn_id": turn_id, "now": _now()})
            connection.execute(text("""UPDATE chat_turns SET cancel_requested=1 WHERE workspace_id=:workspace_id
                AND user_id=:user_id AND turn_id=:turn_id AND status='pending'"""), {**scope, "turn_id": turn_id})

    def reset(self, scope, conversation_id=None):
        with self.transaction(scope) as connection:
            self._expire(connection, scope)
            active = self.active(connection, scope)
            if conversation_id and (not active or active["id"] != conversation_id):
                raise ConversationError("Conversation was not found.", 404, "conversation_not_found")
            if not active:
                return None
            cid = active["id"]
            if connection.execute(text("SELECT turn_id FROM chat_turns WHERE conversation_id=:cid AND status='pending'"), {"cid": cid}).first():
                raise ConversationError("Stop the running request before clearing this conversation.", code="turn_pending")
            counter = self._increment(connection, cid)
            connection.execute(text("UPDATE chat_conversations SET reset_revision=:revision WHERE id=:cid"), {"cid": cid, "revision": counter["revision"]})
            connection.execute(text("DELETE FROM chat_messages WHERE conversation_id=:cid"), {"cid": cid})
            # Keep small receipts to prevent replaying old paid turns, but erase
            # their stored response/graph content when the conversation is cleared.
            connection.execute(text("""UPDATE chat_turns SET status='cancelled',response_json=:response,response_status=409
                WHERE conversation_id=:cid"""), {"cid": cid, "response": json.dumps({"status": "cancelled", "session_id": cid, "assistant_message": "This conversation was cleared."})})
            return cid

    def update_proposal(self, scope, message_id, proposal_status):
        if proposal_status not in {"applied", "discarded"}:
            raise ConversationError("Invalid proposal status.", 400)
        with self.transaction(scope) as connection:
            row = connection.execute(text("""SELECT m.* FROM chat_messages m JOIN chat_conversations c ON c.id=m.conversation_id
                WHERE m.id=:id AND c.workspace_id=:workspace_id AND c.user_id=:user_id"""), {**scope, "id": message_id}).mappings().first()
            if not row:
                raise ConversationError("Message was not found.", 404, "message_not_found")
            if row["proposal_status"] not in {"pending", proposal_status}:
                raise ConversationError("This message has no pending proposal.")
            revision = self._increment(connection, row["conversation_id"])["revision"]
            connection.execute(text("UPDATE chat_messages SET proposal_status=:proposal,revision=:revision WHERE id=:id"),
                               {"id": message_id, "proposal": proposal_status, "revision": revision})

    @contextmanager
    def clear_guard(self, workspace_id):
        # Hold the workspace lease until graph, object storage and checkpoints
        # finish clearing. A new chat cannot start halfway through a reset.
        with self.transaction({"workspace_id": workspace_id}) as connection:
            self._expire(connection, {"workspace_id": workspace_id}, workspace=True)
            if connection.execute(text("SELECT turn_id FROM chat_turns WHERE workspace_id=:ws AND status='pending' LIMIT 1"), {"ws": workspace_id}).first():
                raise ConversationError("Stop active chat requests before clearing this workspace.", code="workspace_chat_busy")
            yield connection

    @staticmethod
    def clear_locked(connection, workspace_id):
        result = connection.execute(text("DELETE FROM chat_conversations WHERE workspace_id=:ws"), {"ws": workspace_id})
        connection.execute(text("DELETE FROM chat_cancel_requests WHERE workspace_id=:ws"), {"ws": workspace_id})
        return result.rowcount


_STORE = None
_LOCATION = None
_STORE_LOCK = threading.Lock()


def conversation_store():
    global _STORE, _LOCATION
    location = (os.getenv("DATABASE_URL", "").strip() if is_auth_enabled() else "") or os.getenv("INLUMEN_CHAT_DB_PATH", "").strip() or str(Path(__file__).parent / "state" / "conversations.sqlite3")
    with _STORE_LOCK:
        if _STORE is None or _LOCATION != location:
            if _STORE is not None:
                _STORE.engine.dispose()
            _STORE = ConversationStore(location)
            _LOCATION = location
        return _STORE
