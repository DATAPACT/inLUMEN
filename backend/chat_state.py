from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from auth_middleware import current_workspace_id
from workspace_store import LOCAL_WORKSPACE_ID


STATE_DIR = Path(os.getenv("CHAT_STATE_DIR", "./state"))
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ENGINE = None
_ENGINE_URL = ""
_ENGINE_LOCK = threading.Lock()


def _session_id(value: str) -> str:
    session_id = str(value or "").strip()
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid chat session id")
    return session_id


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _engine():
    global _ENGINE, _ENGINE_URL
    database_url = _database_url().replace("postgresql://", "postgresql+psycopg://", 1)
    with _ENGINE_LOCK:
        if _ENGINE is not None and _ENGINE_URL == database_url:
            return _ENGINE
        if _ENGINE is not None:
            _ENGINE.dispose()
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.begin() as connection:
            connection.execute(
                text("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, session_id)
            )
            """)
            )
        _ENGINE = engine
        _ENGINE_URL = database_url
        return engine


def state_file(session_id: str) -> Path:
    workspace_id = current_workspace_id()
    workspace_dir = (
        STATE_DIR
        if workspace_id == LOCAL_WORKSPACE_ID
        else STATE_DIR / "chat-sessions" / workspace_id
    )
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir / f"{_session_id(session_id)}.json"


def load_state_from_disk(session_id: str) -> Any:
    session_id = _session_id(session_id)
    if _database_url():
        engine = _engine()
        with engine.connect() as connection:
            row = connection.execute(
                text("""
                SELECT state_json FROM chat_sessions
                WHERE workspace_id=:workspace_id AND session_id=:session_id
            """),
                {
                    "workspace_id": current_workspace_id(),
                    "session_id": session_id,
                },
            ).first()
        return json.loads(str(row[0])) if row is not None else None
    path = state_file(session_id)
    if not path.exists():
        return None
    return json.loads(path.read_text("utf-8"))


def save_state_to_disk(session_id: str, team_state: Any) -> None:
    session_id = _session_id(session_id)
    encoded = json.dumps(team_state, ensure_ascii=False)
    if _database_url():
        engine = _engine()
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO chat_sessions (
                    workspace_id, session_id, state_json, updated_at
                ) VALUES (
                    :workspace_id, :session_id, :state_json, now()
                )
                ON CONFLICT(workspace_id, session_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=now()
            """),
                {
                    "workspace_id": current_workspace_id(),
                    "session_id": session_id,
                    "state_json": encoded,
                },
            )
        return
    state_file(session_id).write_text(encoded, encoding="utf-8")


def clear_state_from_disk(session_id: str) -> None:
    session_id = _session_id(session_id)
    if _database_url():
        engine = _engine()
        with engine.begin() as connection:
            connection.execute(
                text("""
                DELETE FROM chat_sessions
                WHERE workspace_id=:workspace_id AND session_id=:session_id
            """),
                {
                    "workspace_id": current_workspace_id(),
                    "session_id": session_id,
                },
            )
        return
    path = state_file(session_id)
    if path.exists():
        path.unlink()
