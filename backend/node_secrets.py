from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine, text

from auth_middleware import current_workspace_id
from node_parameters import normalize_secret_param_keys


_PARAMETER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
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


def _key_path() -> Path:
    configured = os.getenv("INLUMEN_SECRET_KEY_PATH", "").strip()
    return (
        Path(configured)
        if configured
        else Path(__file__).parent / "state" / "node-secrets.key"
    )


def _fernet() -> Fernet:
    configured_key = os.getenv("INLUMEN_SECRET_ENCRYPTION_KEY", "").strip()
    if configured_key:
        return Fernet(configured_key.encode("ascii"))
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = path.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = path.read_bytes().strip()
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key + b"\n")
    return Fernet(key)


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
            connection.execute(
                text(
                    """
            CREATE TABLE IF NOT EXISTS node_secrets (
                workspace_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                parameter_name TEXT NOT NULL,
                encrypted_value BYTEA NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (workspace_id, node_id, parameter_name)
            )
        """.replace("BYTEA", "BLOB")
                    if engine.dialect.name == "sqlite"
                    else """
            CREATE TABLE IF NOT EXISTS node_secrets (
                workspace_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                parameter_name TEXT NOT NULL,
                encrypted_value BYTEA NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, node_id, parameter_name)
            )
            """
                )
            )
        _ENGINE = engine
        _ENGINE_URL = database_url
        return engine


def normalize_parameter_name(value: Any) -> str:
    name = str(value or "").strip()
    if not _PARAMETER_NAME_RE.fullmatch(name):
        raise ValueError("invalid parameter name")
    return name


def runtime_secret_name(node_id: Any, parameter_name: Any) -> str:
    node_fragment = re.sub(r"[^A-Za-z0-9]+", "_", str(node_id or "")).upper().strip("_")
    parameter_fragment = (
        re.sub(r"[^A-Za-z0-9]+", "_", normalize_parameter_name(parameter_name))
        .upper()
        .strip("_")
    )
    if not node_fragment:
        raise ValueError("node id is required")
    return f"INLUMEN_SECRET_{node_fragment}_{parameter_fragment}"


def set_node_secret(node_id: Any, parameter_name: Any, value: Any) -> None:
    clean_node_id = str(node_id or "").strip()
    if not clean_node_id:
        raise ValueError("node id is required")
    clean_name = normalize_parameter_name(parameter_name)
    clean_value = str(value or "")
    if not clean_value:
        raise ValueError("secret value is required")
    encrypted = _fernet().encrypt(clean_value.encode("utf-8"))
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO node_secrets (
                workspace_id, node_id, parameter_name, encrypted_value, updated_at
            ) VALUES (
                :workspace_id, :node_id, :parameter_name, :encrypted_value, CURRENT_TIMESTAMP
            )
            ON CONFLICT(workspace_id, node_id, parameter_name) DO UPDATE SET
                encrypted_value=excluded.encrypted_value,
                updated_at=CURRENT_TIMESTAMP
        """),
            {
                "workspace_id": current_workspace_id(),
                "node_id": clean_node_id,
                "parameter_name": clean_name,
                "encrypted_value": encrypted,
            },
        )


def delete_node_secret(node_id: Any, parameter_name: Any) -> bool:
    engine = _engine()
    with engine.begin() as connection:
        result = connection.execute(
            text("""
            DELETE FROM node_secrets
            WHERE workspace_id=:workspace_id AND node_id=:node_id
              AND parameter_name=:parameter_name
        """),
            {
                "workspace_id": current_workspace_id(),
                "node_id": str(node_id or "").strip(),
                "parameter_name": normalize_parameter_name(parameter_name),
            },
        )
    return int(result.rowcount or 0) > 0


def clear_node_secrets(node_id: Any | None = None) -> int:
    clean_node_id = str(node_id or "").strip()
    engine = _engine()
    parameters = {"workspace_id": current_workspace_id()}
    node_clause = ""
    if clean_node_id:
        node_clause = " AND node_id=:node_id"
        parameters["node_id"] = clean_node_id
    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM node_secrets WHERE workspace_id=:workspace_id"
                + node_clause
            ),
            parameters,
        ).scalar_one()
        connection.execute(
            text(
                "DELETE FROM node_secrets WHERE workspace_id=:workspace_id"
                + node_clause
            ),
            parameters,
        )
    return int(count)


def configured_node_secrets(node_id: Any) -> list[str]:
    engine = _engine()
    with engine.connect() as connection:
        rows = connection.execute(
            text("""
            SELECT parameter_name FROM node_secrets
            WHERE workspace_id=:workspace_id AND node_id=:node_id
            ORDER BY parameter_name
        """),
            {
                "workspace_id": current_workspace_id(),
                "node_id": str(node_id or "").strip(),
            },
        ).fetchall()
    return [str(row[0]) for row in rows]


def _node_secret_value(node_id: str, parameter_name: str) -> str | None:
    engine = _engine()
    with engine.connect() as connection:
        row = connection.execute(
            text("""
            SELECT encrypted_value FROM node_secrets
            WHERE workspace_id=:workspace_id AND node_id=:node_id
              AND parameter_name=:parameter_name
        """),
            {
                "workspace_id": current_workspace_id(),
                "node_id": node_id,
                "parameter_name": parameter_name,
            },
        ).first()
    if row is None:
        return None
    try:
        return _fernet().decrypt(bytes(row[0])).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"Stored secret {parameter_name!r} for node {node_id!r} could not be decrypted."
        ) from exc


def runtime_secret_environment(graph: Any) -> dict[str, str]:
    if not isinstance(graph, dict):
        return {}
    environment: dict[str, str] = {}
    raw_nodes = list(graph.get("nodes") or [])
    raw_nodes.extend(
        row.get("step")
        for row in (graph.get("step_rows") or [])
        if isinstance(row, dict) and isinstance(row.get("step"), dict)
    )
    for node in raw_nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else node
        node_id = str(
            data.get("flow_id") or node.get("id") or data.get("id") or ""
        ).strip()
        parameters = data.get("param") if isinstance(data.get("param"), dict) else {}
        if not parameters and isinstance(data.get("param_json"), str):
            try:
                decoded = json.loads(data.get("param_json") or "{}")
                parameters = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                parameters = {}
        secret_names = normalize_secret_param_keys(
            data.get("secret_params") or data.get("secret_params_json"), parameters
        )
        for name in secret_names:
            value = _node_secret_value(node_id, name)
            if value is not None:
                environment[runtime_secret_name(node_id, name)] = value
    return environment
