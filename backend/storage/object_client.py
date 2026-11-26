from __future__ import annotations

from typing import Any

from local_api_client import LocalApiResponse, dispatch_flask_request


def dispatch_object_request(
    backend_path: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: Any = None,
    json_payload: Any = None,
    files: Any = None,
    form: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> LocalApiResponse:
    return dispatch_flask_request(
        _minio_app(),
        backend_path,
        method=method,
        params=params,
        data=data,
        json_payload=json_payload,
        files=files,
        form=form,
        headers=headers,
    )


def _minio_app():
    from minio_api import app

    return app


def check_object_health() -> bool:
    return dispatch_object_request("health").ok
