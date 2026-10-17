"""Runtime that bridges an inLUMEN pipeline step to a plugin's REST API.

This file is copied verbatim into every plugin step's container as ``main.py``;
``plugins.runtime`` replaces the ``PLUGIN_SPEC`` line with the step's pinned
specification. It depends only on the Python standard library.

Parameter values are read from ``PIPELINE_PARAM_<NAME>`` / ``INLUMEN_PARAM_<NAME>``
(how inLUMEN injects step parameters and secrets at run time) and then from the
embedded specification. Secret values never appear in the specification.
"""
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

PLUGIN_SPEC = json.loads(os.environ.get("INLUMEN_PLUGIN_SPEC", "{}"))  # __PLUGIN_SPEC__

_IGNORED_INPUT_NAMES = {"input_manifest.json", "output_manifest.json", ".gitkeep"}
_MAX_ERROR_BODY = 2000


class PluginCallError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    """Never follow redirects: urllib would forward credentials to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def _typed_parameters():
    """Typed step parameters injected by inLUMEN runtimes as JSON."""
    merged = {}
    for env_name in ("INLUMEN_PARAMS_JSON", "PIPELINE_PARAMS_JSON"):
        raw = os.getenv(env_name)
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except ValueError:
            continue
        if isinstance(decoded, dict):
            merged.update(decoded)
    return merged


def _env_value(name):
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).upper().strip("_")
    for env_name in (f"PIPELINE_PARAM_{normalized}", f"INLUMEN_PARAM_{normalized}"):
        value = os.getenv(env_name)
        if value not in (None, ""):
            return value
    return None


def _value(name, default=None):
    typed = _typed_parameters().get(name)
    if typed not in (None, ""):
        return typed
    value = _env_value(name)
    if value is not None:
        return value
    value = (PLUGIN_SPEC.get("values") or {}).get(name)
    return default if value in (None, "") else value


def _coerce(declaration, value):
    """Convert environment strings to the declared JSON type."""
    kind = declaration.get("type", "string")
    if not isinstance(value, str):
        return value
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean":
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
            raise ValueError(value)
        if kind in {"object", "array"}:
            return json.loads(value)
    except ValueError:
        raise PluginCallError(
            f"Parameter {declaration['name']!r} must be of type {kind}; got {value!r}."
        ) from None
    return value


def _parameter_values():
    """Resolve declared parameters for the configured operation."""
    resolved = {}
    missing = []
    for declaration in PLUGIN_SPEC.get("parameters") or []:
        value = _value(declaration["name"], declaration.get("default"))
        if value in (None, ""):
            if declaration.get("required"):
                missing.append(declaration["name"])
            continue
        value = _coerce(declaration, value)
        if declaration.get("enum") and value not in declaration["enum"]:
            raise PluginCallError(
                f"Parameter {declaration['name']!r} must be one of {declaration['enum']}."
            )
        resolved[declaration["name"]] = (declaration, value)
    if missing:
        raise PluginCallError(
            f"Plugin {PLUGIN_SPEC['plugin']['name']!r} requires parameters: {', '.join(missing)}."
        )
    return resolved


def _secret(name):
    return _env_value(name)


def _base_url():
    # The endpoint is pinned by the administrator-installed manifest.
    base_url = str(PLUGIN_SPEC.get("base_url") or "").strip().rstrip("/")
    if urlparse(base_url).scheme not in {"http", "https"} or not urlparse(base_url).netloc:
        raise PluginCallError("Plugin endpoint must be an http(s) URL.")
    return base_url


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _wire_name(declaration):
    """Name of a parameter in the HTTP request (query key, header, form or JSON field)."""
    return declaration.get("api_name") or declaration["name"]


def _plugin_name():
    return PLUGIN_SPEC["plugin"]["name"]


def _credentials():
    """Headers, query pairs and form fields that carry the plugin's secrets."""
    headers, query, form = {}, [], []
    for secret in PLUGIN_SPEC.get("secrets") or []:
        value = _secret(secret["name"])
        if secret.get("required") and not value:
            raise PluginCallError(
                f"Plugin {_plugin_name()!r} requires the secret {secret['name']!r}. "
                "Configure it on the step before running."
            )
        if value and secret.get("header"):
            headers[secret["header"]] = value
    auth = PLUGIN_SPEC.get("auth") or {"type": "none"}
    if auth.get("type") in {"api_key", "bearer"}:
        credential = _secret(auth["secret"])
        if not credential:
            raise PluginCallError(
                f"Plugin {_plugin_name()!r} requires the secret {auth['secret']!r}. "
                "Configure it on the step before running."
            )
        if auth.get("query_parameter"):
            query.append((auth["query_parameter"], credential))
        elif auth.get("form_field"):
            form.append((auth["form_field"], credential))
        else:
            prefix = str(auth.get("prefix") or "").strip()
            headers[auth.get("header") or "Authorization"] = f"{prefix} {credential}".strip()
    return headers, query, form


def _build_request(values, body=None, content_type=None, credentials=None):
    operation = PLUGIN_SPEC["operation"]
    path = operation["path"]
    query = []
    headers = {"Accept": "*/*", "User-Agent": "inLUMEN-plugin-adapter/1"}
    for name, (declaration, value) in values.items():
        location = declaration.get("location", "query")
        if location == "path":
            path = path.replace("{" + name + "}", quote(_scalar(value), safe=""))
        elif location == "query":
            query.append((_wire_name(declaration), _scalar(value)))
        elif location == "header":
            headers[_wire_name(declaration)] = _scalar(value)

    secret_headers, secret_query, _ = credentials if credentials else _credentials()
    headers.update(secret_headers)
    query.extend(secret_query)

    unresolved = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path)
    if unresolved:
        raise PluginCallError(
            f"Plugin operation {operation['id']!r} requires path parameters: {', '.join(unresolved)}."
        )
    url = _base_url() + path
    if query:
        url += ("&" if "?" in url else "?") + urlencode(query)
    if content_type:
        headers["Content-Type"] = content_type
    return Request(url, data=body, method=operation["method"], headers=headers)


def _multipart(file_field, filename, payload, fields):
    boundary = f"inlumen-{uuid.uuid4().hex}"
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        lines.append(_scalar(value).encode("utf-8") + b"\r\n")
    safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    file_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    lines.append(f"--{boundary}\r\n".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
        f"Content-Type: {file_type}\r\n\r\n".encode()
    )
    lines.append(payload + b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    return b"".join(lines), f"multipart/form-data; boundary={boundary}"


def _set_dotted(document, dotted, value):
    """Set ``a.b.c`` inside a nested JSON object."""
    parts = dotted.split(".")
    target = document
    for part in parts[:-1]:
        target = target.setdefault(part, {})
        if not isinstance(target, dict):
            raise PluginCallError(f"Request field {dotted!r} conflicts with another parameter.")
    target[parts[-1]] = value


# Text fields of structured upstream results, most specific first: a
# transcript JSON carries "transcript" next to segments, timings and metadata.
TEXT_FIELDS = ("transcript", "text", "content", "body")


def _input_text(input_file):
    """Return the text a plugin should process from one input file.

    Plain text is sent as is. A JSON document is not text to annotate: its
    text field is sent instead of the whole document with its keys, numbers
    and metadata (which can also exceed a tool's size limit).
    """
    try:
        raw = Path(input_file["path"]).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise PluginCallError(
            f"Input {input_file['filename']!r} must be UTF-8 text for this plugin."
        ) from None
    looks_json = str(input_file.get("filename") or "").lower().endswith(".json") or (
        raw.lstrip()[:1] in {"{", "["}
    )
    if not looks_json:
        return raw
    try:
        document = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(document, str):
        return document
    if isinstance(document, dict):
        for field in TEXT_FIELDS:
            value = document.get(field)
            if isinstance(value, str) and value.strip():
                return value
    raise PluginCallError(
        f"Input {input_file['filename']!r} is a JSON document without a text field "
        f"({', '.join(TEXT_FIELDS)}); {_plugin_name()!r} needs text. Connect a step "
        "that produces text, or add a Task that extracts it."
    )


def _timeout(default):
    try:
        return float(_value("timeout", default))
    except (TypeError, ValueError):
        raise PluginCallError("Plugin timeout must be a number of seconds.") from None


def _send(request, timeout, label):
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get_content_type()
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise PluginCallError(
                f"Plugin {_plugin_name()!r} answered with a redirect "
                f"(HTTP {exc.code}); redirects are not followed. Update the plugin endpoint."
            ) from None
        detail = exc.read(_MAX_ERROR_BODY).decode("utf-8", "replace")
        raise PluginCallError(
            f"Plugin {_plugin_name()!r} {label} returned HTTP {exc.code}: {detail}"
        ) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise PluginCallError(
            f"Plugin {_plugin_name()!r} is unreachable at "
            f"{_base_url()}: {getattr(exc, 'reason', exc)}"
        ) from None


def _json_field(document, dotted):
    value = document
    for part in str(dotted).split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _await_job(submitted_payload, credentials, timeout):
    """Poll an asynchronous job until it completes; return its result as JSON bytes."""
    spec = PLUGIN_SPEC["operation"]["async"]
    try:
        submitted = json.loads(submitted_payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise PluginCallError(f"Plugin {_plugin_name()!r} did not return a JSON job reference.") from None
    job_id = _json_field(submitted, spec.get("job_id_field", "job_id"))
    if job_id in (None, ""):
        raise PluginCallError(f"Plugin {_plugin_name()!r} did not return a job id: {json.dumps(submitted)[:500]}")
    poll_path = spec["poll_path"].replace("{job_id}", quote(str(job_id), safe=""))
    headers = {"Accept": "application/json", "User-Agent": "inLUMEN-plugin-adapter/1"}
    headers.update(credentials[0])
    url = _base_url() + poll_path
    if credentials[1]:
        url += ("&" if "?" in url else "?") + urlencode(credentials[1])
    status_field = spec.get("status_field", "status")
    done = set(spec.get("done_values") or ["completed"])
    failed = set(spec.get("failed_values") or ["failed"])
    interval = float(spec.get("interval_seconds") or 2)
    deadline = time.monotonic() + float(spec.get("timeout_seconds") or 600)
    while True:
        _, payload, _ = _send(Request(url, method="GET", headers=headers), timeout, f"job {job_id}")
        try:
            job = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise PluginCallError(f"Plugin {_plugin_name()!r} job status is not JSON.") from None
        status = _json_field(job, status_field)
        if status in done:
            result = _json_field(job, spec["result_field"]) if spec.get("result_field") else job
            return json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
        if status in failed:
            error = _json_field(job, spec.get("error_field", "error"))
            raise PluginCallError(f"Plugin {_plugin_name()!r} job {job_id} failed: {error}")
        if time.monotonic() >= deadline:
            raise PluginCallError(
                f"Plugin {_plugin_name()!r} job {job_id} did not finish in "
                f"{spec.get('timeout_seconds') or 600} seconds (last status: {status})."
            )
        time.sleep(interval)


def _call(values, input_file=None):
    operation = PLUGIN_SPEC["operation"]
    request_spec = operation.get("request") or {}
    body_mode = request_spec.get("body", "none")
    input_field = request_spec.get("input_field")
    body_values = [
        (declaration, value)
        for _, (declaration, value) in values.items()
        if declaration.get("location") == "body"
    ]
    credentials = _credentials()
    body = None
    content_type = None
    if body_mode == "parameters":
        document = {}
        for declaration, value in body_values:
            _set_dotted(document, _wire_name(declaration), value)
        if input_field and input_file is not None:
            _set_dotted(document, input_field, _input_text(input_file))
        body = json.dumps(document).encode("utf-8")
        content_type = "application/json"
    elif body_mode == "form":
        fields = [(_wire_name(declaration), _scalar(value)) for declaration, value in body_values]
        if input_field and input_file is not None:
            fields.append((input_field, _input_text(input_file)))
        fields.extend(credentials[2])
        body = urlencode(fields).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    elif body_mode == "input":
        body = Path(input_file["path"]).read_bytes()
        content_type = (
            request_spec.get("content_type")
            or mimetypes.guess_type(input_file["filename"])[0]
            or "application/octet-stream"
        )
    elif body_mode == "multipart":
        body, content_type = _multipart(
            request_spec.get("file_field") or "file",
            Path(input_file["filename"]).name,
            Path(input_file["path"]).read_bytes(),
            {_wire_name(declaration): value for declaration, value in body_values},
        )
    request = _build_request(values, body, content_type, credentials)
    timeout = _timeout(operation.get("timeout_seconds") or 60)
    status, payload, response_type = _send(request, timeout, f"operation {operation['id']!r}")
    if operation.get("async"):
        return status, _await_job(payload, credentials, timeout), "application/json"
    return status, payload, response_type


def _input_files(input_dir):
    manifest = os.getenv("INLUMEN_INPUT_MANIFEST")
    if manifest and Path(manifest).is_file():
        entries = json.loads(Path(manifest).read_text())["inputs"]
    elif input_dir.is_dir():
        entries = [
            {"filename": path.relative_to(input_dir).as_posix(), "path": str(path)}
            for path in sorted(input_dir.iterdir())
            if path.name not in _IGNORED_INPUT_NAMES
        ]
    else:
        entries = []
    files = []
    for entry in entries:
        path = Path(entry["path"])
        if path.is_dir():
            files.extend(
                {"filename": child.relative_to(path.parent).as_posix(), "path": str(child)}
                for child in sorted(path.rglob("*"))
                if child.is_file() and child.name not in _IGNORED_INPUT_NAMES
            )
        elif path.is_file() and path.name not in _IGNORED_INPUT_NAMES:
            files.append({"filename": str(entry["filename"]), "path": str(path)})
    return files


def _check_payload(payload, operation):
    response_spec = operation.get("response") or {}
    error_fields = response_spec.get("error_fields") or []
    if response_spec.get("format") == "json" or error_fields:
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise PluginCallError(
                f"Plugin operation {operation['id']!r} declared a JSON response but "
                "returned something else."
            ) from None
        if isinstance(document, dict):
            reported = {
                field: document[field]
                for field in error_fields
                if document.get(field) not in (None, "", [], {})
            }
            if reported:
                raise PluginCallError(
                    f"Plugin {PLUGIN_SPEC['plugin']['name']!r} reported an error: "
                    + json.dumps(reported)[:_MAX_ERROR_BODY]
                )
    return payload


def _write_metadata(calls):
    work_dir = os.environ.get("PIPELINE_WORK_DIR")
    if not work_dir:
        return
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    (Path(work_dir) / "plugin-calls.json").write_text(
        json.dumps({
            "plugin": PLUGIN_SPEC["plugin"],
            "operation": PLUGIN_SPEC["operation"]["id"],
            "calls": calls,
        }, indent=2) + "\n",
        encoding="utf-8",
    )


def _safe_name(filename):
    stem = Path(str(filename)).with_suffix("").as_posix()
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "item"


def run(input_dir, output_dir):
    operation = PLUGIN_SPEC["operation"]
    kind = PLUGIN_SPEC["kind"]
    body_mode = (operation.get("request") or {}).get("body", "none")
    response_spec = operation.get("response") or {}
    output_name = str(_value("output_filename", response_spec.get("filename") or "response.json"))
    if "/" in output_name or "\\" in output_name or output_name in {"", ".", ".."}:
        raise PluginCallError("Plugin output filename must be a plain file name.")
    values = _parameter_values()
    per_input = body_mode in {"input", "multipart"} or (
        body_mode in {"form", "parameters"}
        and bool((operation.get("request") or {}).get("input_field"))
    )
    inputs = _input_files(input_dir) if per_input else []
    if per_input and not inputs:
        raise PluginCallError(
            f"Plugin operation {operation['id']!r} sends input files, but the step received none."
        )

    calls = []
    results = []
    for item in (inputs or [None]):
        status, payload, content_type = _call(values, item)
        if kind != "destination":
            payload = _check_payload(payload, operation)
        calls.append({
            "input": item["filename"] if item else None,
            "status": status,
            "content_type": content_type,
            "size_bytes": len(payload),
        })
        results.append((item, payload))
    _write_metadata(calls)

    output_dir.mkdir(parents=True, exist_ok=True)
    if kind == "destination":
        (output_dir / "delivery-receipt.json").write_text(
            json.dumps({
                "plugin": PLUGIN_SPEC["plugin"],
                "operation": operation["id"],
                "delivered": calls,
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        return calls

    if response_spec.get("representation") == "directory":
        bundle = output_dir / output_name
        bundle.mkdir(parents=True, exist_ok=True)
        extension = response_spec.get("format") or "bin"
        used = set()
        for item, payload in results:
            name = _safe_name(item["filename"]) if item else "response"
            candidate, index = name, 2
            while candidate in used:
                candidate, index = f"{name}-{index}", index + 1
            used.add(candidate)
            (bundle / f"{candidate}.{extension}").write_bytes(payload)
        return calls

    if len(results) != 1:
        raise PluginCallError(
            f"Plugin operation {operation['id']!r} returns a single file but received "
            f"{len(results)} input files. Connect one file or use a plugin operation "
            "that returns a directory."
        )
    (output_dir / output_name).write_bytes(results[0][1])
    return calls


def main():
    input_dir = Path(os.environ.get("PIPELINE_INPUT_DIR", "."))
    output_dir = Path(
        os.environ.get("PIPELINE_OUTPUT_DIR") or os.environ.get("INLUMEN_OUTPUT_DIR") or "outputs"
    )
    run(input_dir, output_dir)


if __name__ == "__main__":
    main()
