"""Read and clean up pipeline run outputs that codegen stored in MinIO.

A run output entry from the runner either carries its bytes on the runner
(inline) or references an object (``storage: "minio"``). References are only
honoured when bucket and key are exactly the ones derived from the caller's
workspace, the run id and the output path, so an entry can never point a
request at another workspace's objects.
"""

from __future__ import annotations

from typing import Any, Iterator

from minio.deleteobjects import DeleteObject

from minio_gateway import get_minio_client
from run_artifact_names import RUN_ARTIFACTS_PREFIX, run_artifacts_bucket, run_output_key, run_prefix

STREAM_CHUNK_BYTES = 1024 * 1024


class RunArtifactReferenceError(PermissionError):
    pass


def object_reference(
    entry: dict[str, Any], run_id: str, workspace_id: str
) -> tuple[str, str] | None:
    """Return (bucket, key) for an object-stored output, or None when inline."""
    if entry.get("storage") != "minio":
        return None
    bucket = run_artifacts_bucket(workspace_id)
    try:
        key = run_output_key(run_id, str(entry.get("path") or ""))
    except ValueError as exc:
        raise RunArtifactReferenceError(str(exc)) from exc
    if entry.get("bucket") != bucket or entry.get("key") != key:
        raise RunArtifactReferenceError("Run output reference does not belong to this run.")
    return bucket, key


def read_window(bucket: str, key: str, offset: int, length: int | None) -> tuple[bytes, int]:
    """Read a byte range of an object and return it with the object's size."""
    client = get_minio_client()
    size = int(client.stat_object(bucket, key).size)
    offset = min(max(offset, 0), size)
    if offset >= size or length == 0:
        return b"", size
    response = client.get_object(bucket, key, offset=offset, length=length or 0)
    try:
        return response.read(), size
    finally:
        response.close()
        response.release_conn()


def stream_object(bucket: str, key: str) -> tuple[Iterator[bytes], int]:
    """Stream an object without buffering it whole."""
    client = get_minio_client()
    size = int(client.stat_object(bucket, key).size)
    response = client.get_object(bucket, key)

    def chunks() -> Iterator[bytes]:
        try:
            yield from response.stream(STREAM_CHUNK_BYTES)
        finally:
            response.close()
            response.release_conn()

    return chunks(), size


def remove_run_artifacts(workspace_id: str, run_id: str | None = None) -> int:
    """Delete one run's stored outputs, or every run's in the workspace."""
    client = get_minio_client()
    bucket = run_artifacts_bucket(workspace_id)
    if not client.bucket_exists(bucket):
        return 0
    prefix = run_prefix(run_id) if run_id else f"{RUN_ARTIFACTS_PREFIX}/"
    names = [item.object_name for item in client.list_objects(bucket, prefix=prefix, recursive=True)]
    errors = list(client.remove_objects(bucket, (DeleteObject(name) for name in names)))
    if errors:
        raise RuntimeError(f"Could not delete {len(errors)} run artifact object(s): {errors[0]}")
    return len(names)
