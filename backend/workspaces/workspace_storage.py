from __future__ import annotations

import hashlib
import re

from auth_middleware import current_workspace_id
from workspace_store import LOCAL_WORKSPACE_ID


def _digest(value: str, length: int = 20) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def workspace_bucket_prefix(workspace_id: str | None = None) -> str:
    resolved = workspace_id or current_workspace_id()
    if resolved == LOCAL_WORKSPACE_ID:
        return "files-step-id-"
    return f"files-ws-{_digest(resolved, 16)}-"


def node_bucket_name(node_id: object, workspace_id: str | None = None) -> str:
    resolved = workspace_id or current_workspace_id()
    raw_node_id = str(node_id or "").strip().lower()
    if resolved == LOCAL_WORKSPACE_ID:
        return f"files-step-id-{raw_node_id}"
    readable = re.sub(r"[^a-z0-9-]+", "-", raw_node_id).strip("-")[:20]
    node_fragment = readable or "node"
    return (
        f"{workspace_bucket_prefix(resolved)}{node_fragment}-{_digest(raw_node_id, 8)}"
    )[:63].rstrip("-")


def version_snapshot_bucket(workspace_id: str | None = None) -> str:
    resolved = workspace_id or current_workspace_id()
    if resolved == LOCAL_WORKSPACE_ID:
        return "pipeline-version-file-snapshots"
    return f"pipeline-snapshots-ws-{_digest(resolved, 20)}"


def bucket_belongs_to_workspace(
    bucket_name: object, workspace_id: str | None = None
) -> bool:
    resolved = workspace_id or current_workspace_id()
    bucket = str(bucket_name or "").strip().lower()
    if not bucket:
        return False
    return bucket == version_snapshot_bucket(resolved) or bucket.startswith(
        workspace_bucket_prefix(resolved)
    )
