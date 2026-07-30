from __future__ import annotations

from pathlib import PurePosixPath
from typing import BinaryIO


PROBE_LIMIT_BYTES = 1024 * 1024
_PLACEHOLDER_MARKERS = (
    b"fakepcm",
    b"this is a fake",
    b"placeholder file",
    b"placeholder data",
    b"dummy file",
    b"dummy data",
)


def read_attachment_probe(stream: BinaryIO) -> tuple[bytes, int]:
    """Read a bounded upload probe and restore the stream for storage."""
    original_position = stream.tell()
    try:
        stream.seek(0, 2)
        size_bytes = stream.tell()
        stream.seek(0)
        probe = stream.read(PROBE_LIMIT_BYTES)
    finally:
        stream.seek(original_position)
    return probe, size_bytes


def attachment_input_errors(
    filename: str,
    probe: bytes,
    *,
    size_bytes: int | None = None,
) -> list[str]:
    """Return cheap, deterministic errors for clearly invalid input attachments."""
    name = PurePosixPath(str(filename or "")).name
    extension = PurePosixPath(name).suffix.lower()
    size = len(probe) if size_bytes is None else max(0, int(size_bytes))
    errors: list[str] = []

    lowered = probe[:65536].lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        errors.append(
            f"{name} contains placeholder data instead of a real input file. "
            "Upload the real input; files returned by an AI must contain code only."
        )

    if extension == ".wav":
        errors.extend(_wav_errors(name, probe, size))

    return errors


def _wav_errors(filename: str, probe: bytes, size_bytes: int) -> list[str]:
    if size_bytes < 12 or len(probe) < 12:
        return [
            f"{filename} is not a valid WAV file: the file is too small. "
            "Upload a real audio recording."
        ]

    container = probe[:4]
    if container not in {b"RIFF", b"RF64"} or probe[8:12] != b"WAVE":
        return [
            f"{filename} is not a valid WAV file: its WAV header is missing. "
            "Upload a real audio recording."
        ]

    if container == b"RIFF":
        declared_size = int.from_bytes(probe[4:8], "little") + 8
        if declared_size > size_bytes:
            return [
                f"{filename} is not a valid WAV file: its header claims "
                f"{declared_size} bytes but only {size_bytes} bytes were uploaded. "
                "Upload a real audio recording."
            ]

    found_format = False
    found_audio_data = False
    offset = 12
    while offset + 8 <= len(probe):
        chunk_id = probe[offset:offset + 4]
        chunk_size = int.from_bytes(probe[offset + 4:offset + 8], "little")
        chunk_data_start = offset + 8
        chunk_end = chunk_data_start + chunk_size
        if chunk_end > size_bytes:
            return [
                f"{filename} is not a valid WAV file: a chunk extends past the "
                "end of the uploaded file. Upload a real audio recording."
            ]
        if chunk_id == b"fmt " and chunk_size >= 16:
            found_format = True
        elif chunk_id == b"data" and chunk_size > 0:
            found_audio_data = True
        offset = chunk_end + (chunk_size % 2)
        if offset > len(probe):
            break

    if size_bytes <= len(probe):
        if not found_format:
            return [
                f"{filename} is not a valid WAV file: it has no audio format chunk. "
                "Upload a real audio recording."
            ]
        if not found_audio_data:
            return [
                f"{filename} is not a valid WAV file: it contains no audio samples. "
                "Upload a real audio recording."
            ]
    return []
