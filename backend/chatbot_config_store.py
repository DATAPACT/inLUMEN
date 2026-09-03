from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from auth_middleware import current_workspace_id
from workspace_store import LOCAL_WORKSPACE_ID


_ENGINE = None
_ENGINE_URL = ""
_ENGINE_LOCK = threading.Lock()


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
            CREATE TABLE IF NOT EXISTS chatbot_configurations (
                workspace_id TEXT NOT NULL,
                config_id TEXT NOT NULL,
                config_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, config_id)
            )
            """)
            )
        _ENGINE = engine
        _ENGINE_URL = database_url
        return engine


def load_chatbot_configs(fallback_path: Path) -> list[dict[str, Any]]:
    if _database_url():
        engine = _engine()
        with engine.connect() as connection:
            rows = connection.execute(
                text("""
                SELECT config_json FROM chatbot_configurations
                WHERE workspace_id=:workspace_id
                ORDER BY updated_at DESC, config_id
            """),
                {"workspace_id": current_workspace_id()},
            ).fetchall()
        configs: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row[0]))
            if isinstance(payload, dict):
                configs.append(payload)
        return configs

    workspace_id = current_workspace_id()
    scoped_path = (
        fallback_path
        if workspace_id == LOCAL_WORKSPACE_ID
        else fallback_path.with_name(
            f"{fallback_path.stem}-{workspace_id}{fallback_path.suffix}"
        )
    )
    try:
        payload = json.loads(scoped_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    configs = payload.get("configs") if isinstance(payload, dict) else payload
    return configs if isinstance(configs, list) else []


def save_chatbot_configs(fallback_path: Path, configs: list[dict[str, Any]]) -> None:
    if _database_url():
        engine = _engine()
        workspace_id = current_workspace_id()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM chatbot_configurations WHERE workspace_id=:workspace_id"
                ),
                {"workspace_id": workspace_id},
            )
            for config in configs:
                connection.execute(
                    text("""
                    INSERT INTO chatbot_configurations (
                        workspace_id, config_id, config_json, updated_at
                    ) VALUES (
                        :workspace_id, :config_id, :config_json, now()
                    )
                """),
                    {
                        "workspace_id": workspace_id,
                        "config_id": str(config.get("id") or ""),
                        "config_json": json.dumps(
                            config, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                )
        return

    workspace_id = current_workspace_id()
    scoped_path = (
        fallback_path
        if workspace_id == LOCAL_WORKSPACE_ID
        else fallback_path.with_name(
            f"{fallback_path.stem}-{workspace_id}{fallback_path.suffix}"
        )
    )
    scoped_path.parent.mkdir(parents=True, exist_ok=True)
    scoped_path.write_text(
        json.dumps({"configs": configs}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
