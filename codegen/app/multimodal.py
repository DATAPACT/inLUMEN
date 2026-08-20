from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import urlsplit

from .schemas import FileDescriptor

MAX_MEDIA_PARTS = 6
MAX_DATA_URI_BYTES = 2 * 1024 * 1024


def build_multimodal_user_content(
    payload: dict[str, Any],
    files: list[FileDescriptor],
) -> str | list[dict[str, Any]]:
    """Build a bounded OpenAI-compatible content payload.

    Text metadata is always present. Image/audio parts are additive and callers
    without media samples continue to receive the legacy string content.
    """
    text = json.dumps(payload, indent=2)
    media_parts: list[dict[str, Any]] = []
    for descriptor in files:
        if len(media_parts) >= MAX_MEDIA_PARTS:
            break
        media_parts.extend(
            _parts_for_descriptor(
                descriptor,
                remaining=MAX_MEDIA_PARTS - len(media_parts),
            )
        )
    if not media_parts:
        return text
    return [{"type": "text", "text": text}, *media_parts]


def multimodal_file_summary(files: list[FileDescriptor]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for descriptor in files:
        sample = descriptor.sample
        summaries.append(
            {
                "filename": descriptor.filename,
                "kind": descriptor.kind,
                "format": descriptor.format,
                "content_type": descriptor.content_type,
                "size_bytes": descriptor.size_bytes,
                "columns": descriptor.columns,
                "semantic_role": descriptor.semantic_role,
                "sample": {
                    "row_count": len(sample.rows) if sample else 0,
                    "has_text": bool(sample and sample.text),
                    "has_media": bool(
                        sample
                        and (
                            sample.data_uri
                            or sample.media_url
                            or sample.preview_data_uris
                        )
                    ),
                    "mime_type": sample.mime_type if sample else None,
                    "duration_seconds": sample.duration_seconds if sample else None,
                    "width": sample.width if sample else None,
                    "height": sample.height if sample else None,
                    "metadata": sample.metadata if sample else {},
                },
            }
        )
    return summaries


def _parts_for_descriptor(
    descriptor: FileDescriptor,
    *,
    remaining: int,
) -> list[dict[str, Any]]:
    sample = descriptor.sample
    if sample is None or remaining <= 0:
        return []
    kind = str(descriptor.kind or "").lower()
    file_format = str(descriptor.format or "").lower()
    candidates = [
        item
        for item in [sample.media_url, sample.data_uri, *sample.preview_data_uris]
        if item
    ]
    parts: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(parts) >= remaining:
            break
        if not _safe_media_reference(candidate):
            continue
        if kind == "image" or file_format in {
            "png",
            "jpg",
            "jpeg",
            "webp",
            "gif",
            "bmp",
            "tif",
            "tiff",
            "pdf",
        }:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": candidate, "detail": "low"},
                }
            )
            continue
        if file_format in {"wav", "mp3", "m4a", "aac", "flac", "ogg"}:
            audio_part = _audio_part(candidate, file_format)
            if audio_part:
                parts.append(audio_part)
    return parts


def _safe_media_reference(value: str) -> bool:
    if value.startswith("data:"):
        try:
            _, encoded = value.split(",", 1)
            return len(base64.b64decode(encoded, validate=True)) <= MAX_DATA_URI_BYTES
        except (ValueError, base64.binascii.Error):
            return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _audio_part(value: str, file_format: str) -> dict[str, Any] | None:
    if not value.startswith("data:") or "," not in value:
        return None
    _, encoded = value.split(",", 1)
    return {
        "type": "input_audio",
        "input_audio": {
            "data": encoded,
            "format": "wav" if file_format == "wave" else file_format,
        },
    }
