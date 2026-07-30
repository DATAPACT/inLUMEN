from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any


BASE64_ENCODING = "base64"
TEXT_ENCODING = "utf-8"

_BINARY_EXTENSIONS = {
    ".7z",
    ".aac",
    ".bin",
    ".bmp",
    ".flac",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".joblib",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".npy",
    ".npz",
    ".ogg",
    ".onnx",
    ".parquet",
    ".pdf",
    ".pickle",
    ".pkl",
    ".png",
    ".pt",
    ".pth",
    ".tar",
    ".tif",
    ".tiff",
    ".wav",
    ".webp",
    ".zip",
}
_TEXT_EXTENSIONS = {
    ".cfg",
    ".css",
    ".csv",
    ".graphql",
    ".htm",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".md",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".tsv",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_APPLICATION_TYPES = {
    "application/graphql",
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/sql",
    "application/toml",
    "application/xml",
    "application/x-httpd-php",
    "application/x-javascript",
    "application/x-ndjson",
    "application/x-sh",
    "application/x-yaml",
}


def is_text_artifact(filename: str, content_type: str = "") -> bool:
    path = Path(str(filename or ""))
    suffix = path.suffix.lower()
    if suffix in _BINARY_EXTENSIONS:
        return False
    if suffix in _TEXT_EXTENSIONS:
        return True
    if path.name.lower().startswith("dockerfile"):
        return True

    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type.startswith("text/") or media_type in _TEXT_APPLICATION_TYPES:
        return True
    if media_type.startswith(("audio/", "image/", "video/")):
        return False
    return media_type not in {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/x-7z-compressed",
        "application/x-parquet",
        "application/x-tar",
    }


def content_type_for_filename(filename: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def encode_artifact_bytes(
    content: bytes,
    *,
    filename: str,
    content_type: str = "",
) -> dict[str, Any]:
    resolved_content_type = content_type_for_filename(filename, content_type)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if is_text_artifact(filename, resolved_content_type):
        try:
            text = content.decode(TEXT_ENCODING)
        except UnicodeDecodeError:
            pass
        else:
            return {
                "content": text,
                "content_encoding": TEXT_ENCODING,
                "content_type": resolved_content_type,
                "size_bytes": len(content),
                "sha256": digest,
            }
    return {
        "content": base64.b64encode(content).decode("ascii"),
        "content_encoding": BASE64_ENCODING,
        "content_type": resolved_content_type,
        "size_bytes": len(content),
        "sha256": digest,
    }


def decode_artifact_content(file_entry: dict[str, Any]) -> bytes:
    content = file_entry.get("content")
    text = content if isinstance(content, str) else str(content or "")
    encoding = str(file_entry.get("content_encoding") or TEXT_ENCODING).strip().lower()
    if encoding == BASE64_ENCODING:
        return base64.b64decode(text, validate=True)
    if encoding in {"", TEXT_ENCODING, "text", "plain"}:
        return text.encode(TEXT_ENCODING)
    raise ValueError(f"Unsupported artifact content encoding: {encoding}")


def verify_artifact_integrity(file_entry: dict[str, Any], content: bytes) -> None:
    expected_size = file_entry.get("size_bytes")
    if expected_size is not None and int(expected_size) != len(content):
        raise ValueError(
            f"Artifact size mismatch for {file_entry.get('path') or file_entry.get('filename')}: "
            f"expected {expected_size}, got {len(content)}"
        )
    expected_digest = str(file_entry.get("sha256") or "").strip()
    if expected_digest:
        actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if expected_digest != actual_digest:
            raise ValueError(
                f"Artifact checksum mismatch for "
                f"{file_entry.get('path') or file_entry.get('filename')}"
            )
