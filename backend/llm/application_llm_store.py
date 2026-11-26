"""Singleton shared LLM settings, with encrypted credentials and atomic updates."""
from __future__ import annotations

import json
import threading

from cryptography.fernet import InvalidToken
from sqlalchemy import create_engine, text

from node_secrets import _database_url, _fernet

_ENGINE = None
_ENGINE_URL = ""
_LOCK = threading.Lock()


class ApplicationLLMConflict(ValueError):
    pass


def _engine():
    global _ENGINE, _ENGINE_URL
    url = _database_url()
    with _LOCK:
        if _ENGINE is not None and _ENGINE_URL == url:
            return _ENGINE
        if _ENGINE is not None:
            _ENGINE.dispose()
        engine = create_engine(url, pool_pre_ping=True)
        sql = """
            CREATE TABLE IF NOT EXISTS application_llm_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                config_json TEXT NOT NULL,
                encrypted_key BYTEA NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                revision INTEGER NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """
        with engine.begin() as connection:
            connection.execute(text(sql.replace("BYTEA", "BLOB") if engine.dialect.name == "sqlite" else sql))
        _ENGINE, _ENGINE_URL = engine, url
        return engine


def read_application_llm(*, include_key: bool = False) -> dict:
    with _engine().connect() as connection:
        row = connection.execute(text("SELECT * FROM application_llm_settings WHERE id=1")).mappings().first()
    if row is None:
        return {"config": None, "enabled": False, "revision": 0}
    result = {
        "config": json.loads(row["config_json"]), "enabled": bool(row["enabled"]),
        "revision": row["revision"], "has_api_key": bool(row["encrypted_key"]),
    }
    if include_key and result["enabled"]:
        try:
            result["api_key"] = _fernet().decrypt(bytes(row["encrypted_key"])).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise ValueError("Shared LLM credential cannot be decrypted. Ask an administrator to replace it.") from None
    return result


def save_application_llm(config: dict, api_key: str, *, enabled: bool, revision: int, updated_by: str) -> None:
    """Blank key preserves the stored key. Stale writes never overwrite a newer save."""
    with _engine().begin() as connection:
        existing = connection.execute(text("SELECT * FROM application_llm_settings WHERE id=1")).mappings().first()
        if revision != (existing["revision"] if existing else 0):
            raise ApplicationLLMConflict("Shared LLM settings changed. Close and reopen the form before saving again.")
        if not api_key and existing and json.loads(existing["config_json"])["base_url"] != config["base_url"]:
            raise ValueError("Enter a new API key when changing the provider URL.")
        encrypted = _fernet().encrypt(api_key.encode("utf-8")) if api_key else (existing["encrypted_key"] if existing else None)
        if not encrypted:
            raise ValueError("An API key is required to save the shared LLM configuration.")
        values = {"config": json.dumps(config), "key": encrypted, "enabled": enabled, "revision": revision, "updated_by": updated_by}
        if existing is None:
            result = connection.execute(text("""
                INSERT INTO application_llm_settings (id, config_json, encrypted_key, enabled, revision, updated_by)
                VALUES (1, :config, :key, :enabled, 1, :updated_by) ON CONFLICT(id) DO NOTHING
            """), values)
        else:
            result = connection.execute(text("""
                UPDATE application_llm_settings SET config_json=:config, encrypted_key=:key,
                    enabled=:enabled, revision=revision+1, updated_by=:updated_by, updated_at=CURRENT_TIMESTAMP
                WHERE id=1 AND revision=:revision
            """), values)
        if result.rowcount != 1:
            raise ApplicationLLMConflict("Shared LLM settings changed. Close and reopen the form before saving again.")
