from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from node_parameters import normalize_secret_param_keys


_PARAMETER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _database_path() -> Path:
    configured = os.getenv("INLUMEN_SECRET_DB_PATH", "").strip()
    return Path(configured) if configured else Path(__file__).parent / "state" / "node-secrets.sqlite3"


def _key_path() -> Path:
    configured = os.getenv("INLUMEN_SECRET_KEY_PATH", "").strip()
    return Path(configured) if configured else Path(__file__).parent / "state" / "node-secrets.key"


def _fernet() -> Fernet:
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


def _connection() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS node_secrets (
            node_id TEXT NOT NULL,
            parameter_name TEXT NOT NULL,
            encrypted_value BLOB NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (node_id, parameter_name)
        )
        """
    )
    return connection


@contextmanager
def _open_connection():
    connection = _connection()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def normalize_parameter_name(value: Any) -> str:
    name = str(value or "").strip()
    if not _PARAMETER_NAME_RE.fullmatch(name):
        raise ValueError("invalid parameter name")
    return name


def runtime_secret_name(node_id: Any, parameter_name: Any) -> str:
    node_fragment = re.sub(r"[^A-Za-z0-9]+", "_", str(node_id or "")).upper().strip("_")
    parameter_fragment = re.sub(
        r"[^A-Za-z0-9]+", "_", normalize_parameter_name(parameter_name)
    ).upper().strip("_")
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
    with _open_connection() as connection:
        connection.execute(
            """
            INSERT INTO node_secrets(node_id, parameter_name, encrypted_value, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(node_id, parameter_name) DO UPDATE SET
              encrypted_value=excluded.encrypted_value,
              updated_at=CURRENT_TIMESTAMP
            """,
            (clean_node_id, clean_name, encrypted),
        )


def delete_node_secret(node_id: Any, parameter_name: Any) -> bool:
    clean_node_id = str(node_id or "").strip()
    clean_name = normalize_parameter_name(parameter_name)
    with _open_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM node_secrets WHERE node_id=? AND parameter_name=?",
            (clean_node_id, clean_name),
        )
        return cursor.rowcount > 0


def clear_node_secrets(node_id: Any | None = None) -> int:
    clean_node_id = str(node_id or "").strip()
    with _open_connection() as connection:
        if not clean_node_id:
            count = int(connection.execute("SELECT COUNT(*) FROM node_secrets").fetchone()[0])
            connection.execute("DELETE FROM node_secrets")
            return count
        cursor = connection.execute(
            "DELETE FROM node_secrets WHERE node_id=?",
            (clean_node_id,),
        )
        return cursor.rowcount


def configured_node_secrets(node_id: Any) -> list[str]:
    clean_node_id = str(node_id or "").strip()
    with _open_connection() as connection:
        rows = connection.execute(
            "SELECT parameter_name FROM node_secrets WHERE node_id=? ORDER BY parameter_name",
            (clean_node_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _node_secret_value(node_id: str, parameter_name: str) -> str | None:
    with _open_connection() as connection:
        row = connection.execute(
            "SELECT encrypted_value FROM node_secrets WHERE node_id=? AND parameter_name=?",
            (node_id, parameter_name),
        ).fetchone()
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
        node_id = str(data.get("flow_id") or node.get("id") or data.get("id") or "").strip()
        parameters = data.get("param") if isinstance(data.get("param"), dict) else {}
        if not parameters and isinstance(data.get("param_json"), str):
            try:
                decoded = json.loads(data.get("param_json") or "{}")
                parameters = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                parameters = {}
        secret_names = normalize_secret_param_keys(
            data.get("secret_params") or data.get("secret_params_json"),
            parameters,
        )
        for name in secret_names:
            value = _node_secret_value(node_id, name)
            if value is not None:
                environment[runtime_secret_name(node_id, name)] = value
    return environment
