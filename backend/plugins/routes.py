"""HTTP API for listing, adding and removing plugins.

Plugins are defined by the inLUMEN team from what a tool owner tells us about
their deployed API: either as built-in files in ``backend/plugins/manifests`` or
added in the UI by an application administrator. Read endpoints are available to
every authenticated user. Adding and removing plugins changes what every
workspace can call, so it requires the application administrator role.
"""
from __future__ import annotations

import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import Blueprint, jsonify, make_response, request

from auth_middleware import current_principal, is_application_admin, require_auth

from . import store
from .manifest import PluginManifestError, plugin_summary, validate_plugin_manifest


def _error(status: int, message: str, **extra):
    return jsonify({"error": message, **extra}), status


def _installed_payload(row: dict) -> dict:
    manifest = row["manifest"]
    return {
        **plugin_summary(manifest),
        "source": row.get("source", ""),
        "installed_by": row.get("installed_by", ""),
        "installed_at": row.get("installed_at", ""),
        "manifest": manifest,
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_plugin_endpoint(manifest: dict, *, opener=None, timeout: float = 5.0) -> dict:
    """Probe the tool's health URL (or base URL). Never sends credentials."""
    endpoint = manifest["endpoint"]
    url = endpoint["base_url"].rstrip("/") + (endpoint.get("health_path") or "")
    if urlparse(url).scheme not in {"http", "https"}:
        return {"reachable": False, "url": url, "detail": "Endpoint must use http or https."}
    opener = opener or build_opener(_NoRedirect).open
    started = time.monotonic()
    try:
        with opener(Request(url, method="GET", headers={"User-Agent": "inLUMEN-plugin-check/1"}), timeout=timeout) as response:
            status = response.status
    except HTTPError as exc:
        status = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "reachable": False,
            "url": url,
            "detail": f"Endpoint is unreachable: {getattr(exc, 'reason', exc)}",
        }
    latency_ms = round((time.monotonic() - started) * 1000)
    # Any HTTP answer proves reachability; 5xx means the tool itself is unhealthy.
    healthy = status < 500 if not endpoint.get("health_path") else 200 <= status < 300
    return {
        "reachable": True,
        "healthy": healthy,
        "status": status,
        "latency_ms": latency_ms,
        "url": url,
    }


def create_plugins_blueprint(
    endpoint_checker: Callable[[dict], dict] = check_plugin_endpoint,
) -> Blueprint:
    plugins = Blueprint("plugins", __name__)

    @plugins.route("/api/plugins", methods=["GET", "OPTIONS"])
    @require_auth
    def list_installed():
        if request.method == "OPTIONS":
            return make_response("", 200)
        return jsonify({
            "plugins": [_installed_payload(row) for row in store.list_installed_plugins()],
            "can_manage": is_application_admin(),
        }), 200

    @plugins.route("/api/plugins/<plugin_id>", methods=["GET", "OPTIONS"])
    @require_auth
    def get_installed(plugin_id: str):
        if request.method == "OPTIONS":
            return make_response("", 200)
        row = store.get_installed_plugin(plugin_id)
        if row is None:
            return _error(404, "Plugin is not installed.")
        return jsonify(_installed_payload(row)), 200

    @plugins.route("/api/plugins/<plugin_id>/check", methods=["POST", "OPTIONS"])
    @require_auth
    def check_endpoint(plugin_id: str):
        if request.method == "OPTIONS":
            return make_response("", 200)
        row = store.get_installed_plugin(plugin_id)
        if row is None:
            return _error(404, "Plugin is not installed.")
        return jsonify(endpoint_checker(row["manifest"])), 200

    @plugins.route("/api/admin/plugins", methods=["POST", "OPTIONS"])
    @require_auth
    def add_plugin():
        """Add or update a plugin from its definition: {"manifest": {...}}."""
        if request.method == "OPTIONS":
            return make_response("", 200)
        if not is_application_admin():
            return _error(403, "Application administrator role required.")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("manifest"), dict):
            return _error(400, "Provide the plugin definition as {\"manifest\": {...}}.")
        try:
            manifest = validate_plugin_manifest(payload["manifest"])
        except PluginManifestError as exc:
            return _error(400, "Invalid plugin definition.", details=exc.errors)
        row = store.install_plugin(manifest, installed_by=current_principal().user_id)
        return jsonify(_installed_payload(row)), 201

    @plugins.route("/api/admin/plugins/<plugin_id>", methods=["DELETE", "OPTIONS"])
    @require_auth
    def uninstall(plugin_id: str):
        if request.method == "OPTIONS":
            return make_response("", 200)
        if not is_application_admin():
            return _error(403, "Application administrator role required.")
        if not store.uninstall_plugin(plugin_id):
            if store.is_builtin_plugin(plugin_id):
                return _error(409, "Built-in plugins are defined in backend/plugins/manifests; remove the file there.")
            return _error(404, "Plugin is not installed.")
        return jsonify({"uninstalled": plugin_id}), 200

    return plugins
