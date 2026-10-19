"""Bounded previews of pipeline run outputs.

Previews read byte windows through ``read_window`` so a large output is never
loaded whole. Tables and text are paged by byte offset: each response returns
``next_offset`` to continue from, which keeps paging cheap for any file size.
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator

ReadWindow = Callable[[int, int | None], tuple[bytes, int]]

TABLE_EXTENSIONS = {".csv": ",", ".tsv": "\t", ".tab": "\t"}
JSON_LINES_EXTENSIONS = {".jsonl", ".ndjson"}
JSON_EXTENSIONS = {".json", ".geojson", ".jsonld"}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
TEXT_EXTENSIONS = {
    ".txt", ".log", ".ttl", ".nt", ".nq", ".n3", ".trig", ".rdf", ".owl",
    ".xml", ".yaml", ".yml", ".html", ".htm", ".py", ".sql", ".ini", ".cfg",
    ".toml", ".sparql", ".rq", ".srt", ".vtt",
}
MEDIA_PREFIXES = ("image/", "audio/", "video/")
HEADER_WINDOW_BYTES = 64 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, "")), 1)
    except ValueError:
        return default


def preview_limits() -> dict[str, int]:
    return {
        "max_bytes": _env_int("INLUMEN_PREVIEW_MAX_BYTES", 256 * 1024),
        "max_rows": _env_int("INLUMEN_PREVIEW_MAX_ROWS", 200),
        "json_max_bytes": _env_int("INLUMEN_PREVIEW_JSON_MAX_BYTES", 16 * 1024 * 1024),
        "inline_max_bytes": _env_int("INLUMEN_PREVIEW_INLINE_MAX_BYTES", 25 * 1024 * 1024),
    }


def preview_kind(filename: str, content_type: str = "") -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    content_type = (content_type or "").split(";")[0].strip().lower()
    if suffix in TABLE_EXTENSIONS:
        return "table"
    if suffix in JSON_LINES_EXTENSIONS:
        return "jsonl"
    if suffix in JSON_EXTENSIONS or content_type in {"application/json", "application/ld+json"}:
        return "json"
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    if content_type == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if content_type.startswith(MEDIA_PREFIXES):
        return content_type.split("/", 1)[0]
    if suffix in TEXT_EXTENSIONS or content_type.startswith("text/"):
        return "text"
    return "binary"


def _complete_lines(window: bytes, at_end: bool) -> list[bytes]:
    """Split a window into lines, dropping a trailing partial line."""
    lines = window.splitlines(keepends=True)
    if lines and not at_end and not lines[-1].endswith((b"\n", b"\r")):
        lines.pop()
    return lines


def _infer_type(values: list[str]) -> str:
    present = [value.strip() for value in values if value and value.strip()]
    if not present:
        return "empty"
    if all(re.fullmatch(r"[-+]?\d+", value) for value in present):
        return "integer"
    try:
        for value in present:
            float(value)
        return "number"
    except ValueError:
        pass
    if all(value.lower() in {"true", "false"} for value in present):
        return "boolean"
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ][\d:.+\-Z]*)?", value) for value in present):
        return "date"
    return "string"


def _parse_csv_lines(
    lines: list[bytes], delimiter: str, max_rows: int
) -> tuple[list[list[str]], int]:
    """Parse whole CSV records from lines; return rows and bytes consumed."""
    consumed_per_row: list[int] = []
    counter = {"bytes": 0, "index": 0}

    def feed() -> Iterator[str]:
        while counter["index"] < len(lines):
            line = lines[counter["index"]]
            counter["index"] += 1
            counter["bytes"] += len(line)
            yield line.decode("utf-8", errors="replace")

    rows: list[list[str]] = []
    # Strict mode raises on a quoted value cut off by the window instead of
    # returning half a record, so that record moves to the next page.
    reader = csv.reader(feed(), delimiter=delimiter, strict=True)
    try:
        for row in reader:
            rows.append(row)
            consumed_per_row.append(counter["bytes"])
            if len(rows) >= max_rows:
                break
    except csv.Error:
        pass
    return rows, (consumed_per_row[-1] if consumed_per_row else 0)


def _table_preview(
    read_window: ReadWindow, filename: str, offset: int, limit: int, limits: dict[str, int]
) -> dict[str, Any]:
    delimiter = TABLE_EXTENSIONS[PurePosixPath(filename).suffix.lower()]
    header_window, size = read_window(0, HEADER_WINDOW_BYTES)
    header_lines = _complete_lines(header_window, len(header_window) >= size)
    header_rows, header_bytes = _parse_csv_lines(header_lines, delimiter, 1)
    columns = header_rows[0] if header_rows else []
    if columns and columns[0].startswith("﻿"):
        columns[0] = columns[0][1:]
    start = max(offset, header_bytes)
    window, size = read_window(start, limits["max_bytes"])
    at_end = start + len(window) >= size
    rows, consumed = _parse_csv_lines(_complete_lines(window, at_end), delimiter, limit)
    next_offset = start + consumed
    has_more = next_offset < size
    if not rows and has_more:
        # One record is larger than the preview window; show it as text instead.
        return _text_preview(read_window, start, limits["max_bytes"], "text")
    width = max([len(columns), *(len(row) for row in rows)] or [0])
    columns = [*columns, *(f"column_{index + 1}" for index in range(len(columns), width))]
    return {
        "kind": "table",
        "columns": [
            {"name": name, "type": _infer_type([row[index] if index < len(row) else "" for row in rows])}
            for index, name in enumerate(columns)
        ],
        "rows": rows,
        "offset": start,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "size_bytes": size,
    }


def _json_lines_preview(
    read_window: ReadWindow, offset: int, limit: int, limits: dict[str, int]
) -> dict[str, Any]:
    window, size = read_window(offset, limits["max_bytes"])
    at_end = offset + len(window) >= size
    lines = _complete_lines(window, at_end)
    records: list[Any] = []
    consumed = 0
    for line in lines:
        if len(records) >= limit:
            break
        consumed += len(line)
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            records.append(json.loads(text))
        except ValueError:
            records.append({"value": text})
    columns: list[str] = []
    for record in records:
        for key in record if isinstance(record, dict) else ["value"]:
            if key not in columns:
                columns.append(key)

    def cell(record: Any, key: str) -> str:
        value = record.get(key) if isinstance(record, dict) else record
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    rows = [[cell(record, key) for key in columns] for record in records]
    next_offset = offset + consumed
    has_more = next_offset < size
    return {
        "kind": "table",
        "columns": [
            {"name": key, "type": _infer_type([row[index] for row in rows])}
            for index, key in enumerate(columns)
        ],
        "rows": rows,
        "offset": offset,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "size_bytes": size,
    }


def _text_preview(read_window: ReadWindow, offset: int, limit: int, kind: str) -> dict[str, Any]:
    window, size = read_window(offset, limit)
    at_end = offset + len(window) >= size
    if not at_end:
        cut = window.rfind(b"\n")
        if cut > 0:
            window = window[: cut + 1]
    next_offset = offset + len(window)
    has_more = next_offset < size
    text = window.decode("utf-8", errors="replace")
    if offset == 0 and text.startswith("﻿"):
        text = text[1:]
    return {
        "kind": kind,
        "text": text,
        "offset": offset,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "size_bytes": size,
    }


def _looks_like_text(sample: bytes) -> bool:
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A window may end inside a multi-byte character.
        return exc.start >= len(sample) - 3
    return True


def build_preview(
    read_window: ReadWindow,
    entry: dict[str, Any],
    *,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a bounded, JSON-serialisable preview of one run output."""
    limits = preview_limits()
    filename = str(entry.get("filename") or PurePosixPath(str(entry.get("path") or "")).name)
    content_type = str(entry.get("content_type") or "")
    kind = preview_kind(filename, content_type)
    offset = max(int(offset or 0), 0)
    base = {
        "path": entry.get("path"),
        "filename": filename,
        "content_type": content_type or "application/octet-stream",
        "source_node": entry.get("source_node"),
        "port": entry.get("port"),
    }

    if kind == "table":
        rows = min(max(int(limit or limits["max_rows"]), 1), limits["max_rows"])
        return {**base, **_table_preview(read_window, filename, offset, rows, limits)}
    if kind == "jsonl":
        rows = min(max(int(limit or limits["max_rows"]), 1), limits["max_rows"])
        return {**base, **_json_lines_preview(read_window, offset, rows, limits)}
    if kind == "json" and offset == 0:
        # Check the size first so a file over the limit is never read whole.
        _window, size = read_window(0, 0)
        if size <= limits["json_max_bytes"]:
            window, size = read_window(0, size)
            try:
                value = json.loads(window.decode("utf-8-sig"))
            except ValueError:
                pass
            else:
                return {**base, "kind": "json", "value": value, "size_bytes": size,
                        "offset": 0, "next_offset": None, "has_more": False}
    if kind in {"json", "markdown", "text"}:
        byte_limit = min(max(int(limit or limits["max_bytes"]), 1), limits["max_bytes"])
        return {**base, **_text_preview(read_window, offset, byte_limit, "text" if kind == "json" else kind)}
    if kind in {"image", "audio", "video", "pdf"}:
        _window, size = read_window(0, 0)
        return {**base, "kind": kind, "size_bytes": size,
                "inline": size <= limits["inline_max_bytes"]}

    sample, size = read_window(0, 4096)
    if sample and _looks_like_text(sample):
        byte_limit = min(max(int(limit or limits["max_bytes"]), 1), limits["max_bytes"])
        return {**base, **_text_preview(read_window, offset, byte_limit, "text")}
    return {**base, "kind": "binary", "size_bytes": size}
