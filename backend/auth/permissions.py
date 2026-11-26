"""One authorization policy for gateway and internal adapters."""
import re

ROLE_PERMISSIONS = {
    "owner": {"read", "edit", "run", "admin"},
    "editor": {"read", "edit", "run"},
    "runner": {"read", "run"},
    "viewer": {"read"},
}


def required_permission(method: str, path: str) -> str:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "read"
    if path == "/api/workspace/clear-all" or (path == "/api/pipeline-runs" and method == "DELETE"):
        return "admin"
    if re.fullmatch(r"/api/pipeline-runs(?:/[^/]+)?", path):
        return "run"
    return "edit"


def permits(role: str, method: str, path: str) -> bool:
    return required_permission(method, path) in ROLE_PERMISSIONS.get(role, set())
