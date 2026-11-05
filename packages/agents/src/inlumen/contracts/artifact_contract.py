import re
from dataclasses import asdict, dataclass
from pathlib import PurePath
from typing import Any, Iterable


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
AUDIO_FORMATS = frozenset({"aac", "flac", "m4a", "mp3", "ogg", "opus", "wav"})
VIDEO_FORMATS = frozenset({"avi", "mkv", "mov", "mp4", "mpeg", "mpg", "webm"})
DOCUMENT_FORMATS = frozenset({"doc", "docx", "odp", "odt", "pdf", "ppt", "pptx"})
CANONICAL_ARTIFACT_KINDS = frozenset(
    {
        "table",
        "json",
        "text",
        "image",
        "audio",
        "video",
        "document",
        "model",
        "directory",
        "binary",
    }
)

_PORT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def normalize_port_id(value: Any, fallback: str = "artifact") -> str:
    """Return a path-safe, stable port identifier."""
    normalized = _PORT_ID_RE.sub("-", str(value or "").strip()).strip("-.")
    return normalized or fallback


@dataclass(frozen=True)
class ArtifactBinding:
    """A single engine-neutral output-port to input-port connection."""

    source_node: str
    source_port: str
    target_node: str
    target_port: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def artifact_bindings(edges: Iterable[dict[str, Any]]) -> list[ArtifactBinding]:
    """Normalize and deterministically order graph artifact bindings."""
    bindings: set[ArtifactBinding] = set()
    for edge in edges:
        source_node = str(edge.get("source") or "").strip()
        target_node = str(edge.get("target") or "").strip()
        if not source_node or not target_node or source_node == target_node:
            continue
        bindings.add(
            ArtifactBinding(
                source_node=source_node,
                source_port=normalize_port_id(edge.get("source_port"), "output"),
                target_node=target_node,
                target_port=normalize_port_id(edge.get("target_port"), "input"),
            )
        )
    return sorted(
        bindings,
        key=lambda item: (
            item.target_node,
            item.target_port,
            item.source_node,
            item.source_port,
        ),
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
    if normalized in AUDIO_FORMATS:
        return "audio"
    if normalized in VIDEO_FORMATS:
        return "video"
    if normalized in DOCUMENT_FORMATS:
        return "document"
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
