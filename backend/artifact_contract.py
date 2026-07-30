from pathlib import PurePath
from typing import Any


TABLE_FORMATS = frozenset(
    {"csv", "tsv", "parquet", "xlsx", "xls", "arrow", "feather"}
)
JSON_FORMATS = frozenset({"json", "jsonl", "ndjson"})
TEXT_FORMATS = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "xml",
        "yaml",
        "yml",
        "html",
        "htm",
        "rtf",
        "log",
    }
)
IMAGE_FORMATS = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}
)
CANONICAL_ARTIFACT_KINDS = frozenset(
    {"table", "json", "text", "image", "model", "directory", "binary"}
)


def normalize_artifact_format(filename: Any, file_format: Any = "") -> str:
    declared = str(file_format or "").strip().lower().lstrip(".")
    if declared:
        return declared
    suffix = PurePath(str(filename or "").strip()).suffix.lower().lstrip(".")
    return suffix or "binary"


def artifact_kind_for_format(file_format: Any) -> str:
    normalized = str(file_format or "").strip().lower().lstrip(".")
    if normalized in TABLE_FORMATS:
        return "table"
    if normalized in JSON_FORMATS:
        return "json"
    if normalized in TEXT_FORMATS:
        return "text"
    if normalized in IMAGE_FORMATS:
        return "image"
    return "binary"


def classify_artifact(
    filename: Any,
    *,
    kind: Any = "",
    file_format: Any = "",
) -> dict[str, str]:
    normalized_format = normalize_artifact_format(filename, file_format)
    declared_kind = str(kind or "").strip().lower()
    normalized_kind = (
        declared_kind
        if declared_kind in CANONICAL_ARTIFACT_KINDS
        else artifact_kind_for_format(normalized_format)
    )
    return {"kind": normalized_kind, "format": normalized_format}
