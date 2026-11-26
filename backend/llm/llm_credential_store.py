"""Encrypted, workspace-scoped storage for saved LLM provider credentials.

The API deliberately exposes only whether a configuration has a credential.
The plaintext is decrypted only inside the gateway when it is about to make an
LLM request; it is never returned to the browser or written to configuration
JSON.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from cryptography.fernet import InvalidToken
from sqlalchemy import create_engine, text

from auth_middleware import current_workspace_id
from node_secrets import _fernet


_ENGINE = None
_ENGINE_URL = ""
_ENGINE_LOCK = threading.Lock()


def _database_location() -> str:
    return (
        os.getenv("DATABASE_URL", "").strip()
        or os.getenv("INLUMEN_SECRET_DB_PATH", "").strip()
        or str(Path(__file__).parent / "state" / "node-secrets.sqlite3")
    )


def _database_url() -> str:
    location = _database_location()
    if "://" in location:
        return location.replace("postgresql://", "postgresql+psycopg://", 1)
    path = Path(location).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{path}"


def _engine():
    global _ENGINE, _ENGINE_URL
    database_url = _database_url()
    with _ENGINE_LOCK:
        if _ENGINE is not None and _ENGINE_URL == database_url:
            return _ENGINE
        if _ENGINE is not None:
            _ENGINE.dispose()
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS llm_credentials (
                    workspace_id TEXT NOT NULL,
                    config_id TEXT NOT NULL,
                    encrypted_value BYTEA NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (workspace_id, config_id)
                )
            """.replace("BYTEA", "BLOB") if engine.dialect.name == "sqlite" else """
                CREATE TABLE IF NOT EXISTS llm_credentials (
                    workspace_id TEXT NOT NULL,
                    config_id TEXT NOT NULL,
                    encrypted_value BYTEA NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (workspace_id, config_id)
                )
            """))
        _ENGINE = engine
        _ENGINE_URL = database_url
        return engine


def save_llm_credential(config_id: str, api_key: str) -> None:
    """Save a non-empty credential. Empty updates preserve the current key."""
    clean_id, clean_key = str(config_id or "").strip(), str(api_key or "").strip()
    if not clean_id:
        raise ValueError("configuration id is required")
    if not clean_key:
        return
    encrypted = _fernet().encrypt(clean_key.encode("utf-8"))
    with _engine().begin() as connection:
        connection.execute(text("""
            INSERT INTO llm_credentials (workspace_id, config_id, encrypted_value, updated_at)
            VALUES (:workspace_id, :config_id, :encrypted_value, CURRENT_TIMESTAMP)
            ON CONFLICT(workspace_id, config_id) DO UPDATE SET
                encrypted_value=excluded.encrypted_value,
                updated_at=CURRENT_TIMESTAMP
        """), {
            "workspace_id": current_workspace_id(),
            "config_id": clean_id,
            "encrypted_value": encrypted,
        })


def get_llm_credential(config_id: str) -> str | None:
    clean_id = str(config_id or "").strip()
    if not clean_id:
        return None
    with _engine().connect() as connection:
        row = connection.execute(text("""
            SELECT encrypted_value FROM llm_credentials
            WHERE workspace_id=:workspace_id AND config_id=:config_id
        """), {"workspace_id": current_workspace_id(), "config_id": clean_id}).fetchone()
    if row is None:
        return None
    try:
        return _fernet().decrypt(bytes(row[0])).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError("Saved LLM credential cannot be decrypted.") from exc


def has_llm_credential(config_id: str) -> bool:
    clean_id = str(config_id or "").strip()
    if not clean_id:
        return False
    with _engine().connect() as connection:
        return connection.execute(text("""
            SELECT 1 FROM llm_credentials
            WHERE workspace_id=:workspace_id AND config_id=:config_id
        """), {
            "workspace_id": current_workspace_id(),
            "config_id": clean_id,
        }).fetchone() is not None


def delete_llm_credential(config_id: str) -> None:
    with _engine().begin() as connection:
        connection.execute(text("""
            DELETE FROM llm_credentials
            WHERE workspace_id=:workspace_id AND config_id=:config_id
        """), {"workspace_id": current_workspace_id(), "config_id": str(config_id or "").strip()})
