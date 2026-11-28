"""The CORS and request diagnostics hooks that the backend Flask apps share."""
import sys
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask_setup import install_cors, install_request_observability  # noqa: E402


def _after_request_names(app):
    return [hook.__name__ for hook in app.after_request_funcs.get(None, [])]


def test_install_cors_adds_the_cors_headers(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    app = Flask(__name__)
    install_request_observability(app)
    install_cors(app)
    app.add_url_rule("/ping", view_func=lambda: "ok")

    response = app.test_client().get("/ping", headers={"X-Request-ID": "abc-1"})

    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "X-Request-ID" in response.headers["Access-Control-Allow-Headers"]
    assert response.headers["X-Request-ID"] == "abc-1"
    assert response.headers["Cache-Control"] == "no-store"


def test_the_storage_app_installs_cors_only():
    from storage.minio_api import app

    assert _after_request_names(app) == ["apply_cors"]


def test_the_graph_app_registers_cors_after_the_revision_hook():
    # Flask runs these hooks in reverse order, so apply_cors runs first.
    from graph.neo4j_api import app

    assert _after_request_names(app) == ["finish", "graph_revision_headers", "apply_cors"]
