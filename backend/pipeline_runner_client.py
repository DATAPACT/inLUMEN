from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from auth_middleware import current_principal
from workspace_store import WORKSPACE_HEADER

RUNNER_SERVICE_URL = os.getenv(
    "INLUMEN_RUNNER_SERVICE_URL",
    "http://127.0.0.1:8020",
).rstrip("/")
RUNNER_SERVICE_API_KEY = os.getenv("INLUMEN_RUNNER_SERVICE_API_KEY", "").strip()
RUNNER_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv("INLUMEN_RUNNER_REQUEST_TIMEOUT_SECONDS", "10")
)


class PipelineRunnerError(RuntimeError):
    def __init__(self, status_code: int, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


def runner_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not RUNNER_SERVICE_API_KEY:
        raise PipelineRunnerError(
            503, "Pipeline runner authentication is not configured."
        )
    url = f"{RUNNER_SERVICE_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    encoded = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {RUNNER_SERVICE_API_KEY}",
        WORKSPACE_HEADER: current_principal().workspace_id,
    }
    if payload is not None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    http_request = Request(url, data=encoded, headers=headers, method=method)
    try:
        with urlopen(http_request, timeout=RUNNER_REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            status_code = response.status
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise _runner_error(exc.code, body) from exc
    except URLError as exc:
        raise PipelineRunnerError(
            503,
            f"Pipeline runner is unavailable at {RUNNER_SERVICE_URL}.",
            str(exc.reason),
        ) from exc
    try:
        parsed = json.loads(body) if body else {}
    except ValueError as exc:
        raise PipelineRunnerError(
            502,
            "Pipeline runner returned an invalid response.",
            {"status": status_code},
        ) from exc
    if not isinstance(parsed, dict):
        raise PipelineRunnerError(
            502, "Pipeline runner returned a non-object response."
        )
    return parsed


def runner_raw_request(method: str, path: str) -> tuple[bytes, str, str | None]:
    if not RUNNER_SERVICE_API_KEY:
        raise PipelineRunnerError(
            503, "Pipeline runner authentication is not configured."
        )
    request = Request(
        f"{RUNNER_SERVICE_URL}{path}",
        headers={
            "Accept": "*/*",
            "Authorization": f"Bearer {RUNNER_SERVICE_API_KEY}",
            WORKSPACE_HEADER: current_principal().workspace_id,
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=RUNNER_REQUEST_TIMEOUT_SECONDS) as response:
            return (
                response.read(),
                response.headers.get_content_type(),
                response.headers.get("Content-Disposition"),
            )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise _runner_error(exc.code, body) from exc
    except URLError as exc:
        raise PipelineRunnerError(
            503,
            f"Pipeline runner is unavailable at {RUNNER_SERVICE_URL}.",
            str(exc.reason),
        ) from exc


def _runner_error(status_code: int, body: str) -> PipelineRunnerError:
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = str(detail.get("message") or "Pipeline runner rejected the request.")
        details = detail
    else:
        message = str(detail or "Pipeline runner rejected the request.")
        details = payload or body
    return PipelineRunnerError(status_code, message, details)
