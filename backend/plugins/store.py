"""Installation-wide registry of plugins.

Plugins come from two places, both defined by the inLUMEN team from what a
tool owner tells us about their API:

* built-in: ``*.plugin.json`` files in ``backend/plugins/manifests`` (or the
  folder in ``INLUMEN_PLUGIN_DIR``), version-controlled with the code;
* added by an application administrator in the UI (stored in the database),
  for tools that should not wait for a release. A database entry with the same
  id overrides the built-in definition.

Plugins are available to every workspace. Deployments use the definition that
is active when the bundle is built.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from .manifest import PluginManifestError, validate_plugin_manifest

LOGGER = logging.getLogger(__name__)
BUILTIN_SOURCE = "built-in"
MANUAL_SOURCE = "manual"

_ENGINE = None
_ENGINE_URL = ""
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"at": 0.0, "url": "", "rows": None}
# Short TTL keeps multiple backend replicas consistent without a pub/sub.
CACHE_TTL_SECONDS = 5.0


def _database_url() -> str:
    from node_secrets import _database_url as shared_database_url

    return shared_database_url()


def _engine():
    global _ENGINE, _ENGINE_URL
    url = _database_url()
    with _LOCK:
        if _ENGINE is not None and _ENGINE_URL == url:
            return _ENGINE
        if _ENGINE is not None:
            _ENGINE.dispose()
        engine = create_engine(url, pool_pre_ping=True)
        timestamp = "TIMESTAMP" if engine.dialect.name == "sqlite" else "TIMESTAMPTZ"
        with engine.begin() as connection:
            connection.execute(text(f"""
                CREATE TABLE IF NOT EXISTS installed_plugins (
                    plugin_id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    installed_by TEXT NOT NULL,
                    installed_at {timestamp} NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
        _ENGINE, _ENGINE_URL = engine, url
        _invalidate()
        return engine


def _invalidate() -> None:
    _CACHE.update({"at": 0.0, "rows": None})


def _rows() -> list[dict[str, Any]]:
    url = _database_url()
    now = time.monotonic()
    if (
        _CACHE["rows"] is not None
        and _CACHE["url"] == url
        and now - _CACHE["at"] < CACHE_TTL_SECONDS
    ):
        return _CACHE["rows"]
    with _engine().connect() as connection:
        result = connection.execute(text(
            "SELECT plugin_id, version, manifest_json, source, installed_by, installed_at "
            "FROM installed_plugins ORDER BY plugin_id"
        )).mappings().all()
    rows = []
    for row in result:
        try:
            manifest = json.loads(row["manifest_json"])
        except (TypeError, ValueError):
            continue
        rows.append({
            "id": row["plugin_id"],
            "version": row["version"],
            "source": row["source"],
            "installed_by": row["installed_by"],
            "installed_at": str(row["installed_at"]),
            "manifest": manifest,
        })
    _CACHE.update({"at": now, "url": url, "rows": rows})
    return rows


def plugin_dir() -> Path:
    configured = os.getenv("INLUMEN_PLUGIN_DIR", "").strip()
    return Path(configured) if configured else Path(__file__).with_name("manifests")


_BUILTIN_CACHE: dict[str, Any] = {"key": None, "rows": []}


def _builtin_rows() -> list[dict[str, Any]]:
    """Validated manifests from the plugin folder; invalid files are skipped and logged."""
    folder = plugin_dir()
    files = sorted(folder.glob("*.plugin.json")) if folder.is_dir() else []
    key = (str(folder), tuple((str(p), p.stat().st_mtime_ns) for p in files))
    if _BUILTIN_CACHE["key"] == key:
        return _BUILTIN_CACHE["rows"]
    rows = []
    for path in files:
        try:
            manifest = validate_plugin_manifest(path.read_text(encoding="utf-8"))
        except (OSError, PluginManifestError) as exc:
            LOGGER.error("Skipping invalid plugin file %s: %s", path.name, exc)
            continue
        rows.append({
            "id": manifest["id"],
            "version": manifest["version"],
            "source": BUILTIN_SOURCE,
            "installed_by": "",
            "installed_at": "",
            "manifest": manifest,
        })
    _BUILTIN_CACHE.update({"key": key, "rows": rows})
    return rows


def _all_rows() -> list[dict[str, Any]]:
    merged = {row["id"]: row for row in _builtin_rows()}
    try:
        merged.update({row["id"]: row for row in _rows()})
    except Exception as exc:  # database outage must not hide built-in plugins
        LOGGER.warning("Plugin database unavailable: %s", exc)
    return [merged[key] for key in sorted(merged)]


def list_installed_plugins() -> list[dict[str, Any]]:
    return [dict(row) for row in _all_rows()]


def get_installed_plugin(plugin_id: str) -> dict[str, Any] | None:
    wanted = str(plugin_id or "").strip().lower()
    for row in _all_rows():
        if row["id"] == wanted:
            return dict(row)
    return None


def is_builtin_plugin(plugin_id: str) -> bool:
    wanted = str(plugin_id or "").strip().lower()
    return any(row["id"] == wanted for row in _builtin_rows())


def get_installed_manifest(plugin_id: str) -> dict[str, Any] | None:
    row = get_installed_plugin(plugin_id)
    return row["manifest"] if row else None


def install_plugin(manifest: Any, *, installed_by: str, source: str = MANUAL_SOURCE) -> dict[str, Any]:
    """Add or update an administrator-defined plugin. Validated again on write."""
    normalized = validate_plugin_manifest(manifest)
    values = {
        "plugin_id": normalized["id"],
        "version": normalized["version"],
        "manifest_json": json.dumps(normalized, sort_keys=True),
        "source": str(source or "manual")[:200],
        "installed_by": str(installed_by or "unknown")[:200],
    }
    with _engine().begin() as connection:
        connection.execute(text("""
            INSERT INTO installed_plugins (plugin_id, version, manifest_json, source, installed_by, installed_at)
            VALUES (:plugin_id, :version, :manifest_json, :source, :installed_by, CURRENT_TIMESTAMP)
            ON CONFLICT(plugin_id) DO UPDATE SET
                version=excluded.version,
                manifest_json=excluded.manifest_json,
                source=excluded.source,
                installed_by=excluded.installed_by,
                installed_at=CURRENT_TIMESTAMP
        """), values)
    _invalidate()
    return get_installed_plugin(normalized["id"]) or {}


def uninstall_plugin(plugin_id: str) -> bool:
    """Remove an administrator-defined plugin (built-in files stay)."""
    with _engine().begin() as connection:
        result = connection.execute(
            text("DELETE FROM installed_plugins WHERE plugin_id=:plugin_id"),
            {"plugin_id": str(plugin_id or "").strip().lower()},
        )
    _invalidate()
    return int(result.rowcount or 0) > 0
