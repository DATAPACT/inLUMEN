from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from flask import Flask


class LocalApiHTTPError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalApiResponse:
    content: bytes
    status_code: int
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise LocalApiHTTPError(f"{self.status_code} - {self.text}")


def dispatch_flask_request(
    app: Flask,
    backend_path: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: Any = None,
    json_payload: Any = None,
    files: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> LocalApiResponse:
    request_kwargs: dict[str, Any] = {
        "path": f"/{backend_path.lstrip('/')}",
        "method": method,
        "headers": headers or {},
    }
    if params is not None:
        request_kwargs["query_string"] = params

    if json_payload is not None:
        request_kwargs["json"] = json_payload
    elif files is not None:
        multipart_data = dict(form or {})
        for field_name, file_value in files.items():
            multipart_data[field_name] = _file_tuple_for_flask(file_value)
        request_kwargs["data"] = multipart_data
    elif form is not None:
        request_kwargs["data"] = form
    elif data is not None:
        request_kwargs["data"] = data

    with app.test_client() as client:
        response = client.open(**request_kwargs)

    return LocalApiResponse(
        content=response.get_data(),
        status_code=response.status_code,
        headers=dict(response.headers.items()),
    )


def _file_tuple_for_flask(file_value: Any) -> Any:
    if not isinstance(file_value, tuple):
        return file_value

    if len(file_value) == 3:
        filename, stream, mimetype = file_value
        _rewind_if_possible(stream)
        return (stream, filename, mimetype)

    if len(file_value) == 2:
        filename, stream = file_value
        _rewind_if_possible(stream)
        return (stream, filename)

    return file_value


def _rewind_if_possible(stream: Any) -> None:
    try:
        stream.seek(0)
    except (AttributeError, OSError):
        return
