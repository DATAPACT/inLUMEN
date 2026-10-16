"""Upload pipeline run outputs to MinIO and hand back references.

Outputs then travel to the runner and backend as small references instead of
base64 content, and previews read byte ranges straight from object storage.
When storage is not configured, callers keep the inline behaviour.
"""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

from .run_artifact_names import run_artifacts_bucket, run_output_key, step_receipt_key

LOGGER = logging.getLogger(__name__)


class ObjectClient(Protocol):
    def bucket_exists(self, bucket_name: str) -> bool: ...
    def make_bucket(self, bucket_name: str) -> None: ...
    def fput_object(self, bucket_name: str, object_name: str, file_path: str, content_type: str = ...) -> Any: ...
    def put_object(self, bucket_name: str, object_name: str, data: Any, length: int, content_type: str = ...) -> Any: ...


def _setting(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


class RunArtifactStore:
    def __init__(self, client: ObjectClient, *, retention_days: int | None = None) -> None:
        self.client = client
        self.retention_days = retention_days
        self._ready_buckets: set[str] = set()

    @classmethod
    def from_environment(cls) -> "RunArtifactStore | None":
        if _setting("INLUMEN_RUN_ARTIFACT_STORAGE").lower() != "minio":
            return None
        endpoint = _setting("MINIO_ENDPOINT")
        access_key = _setting("MINIO_ACCESS_KEY", "MINIO_ROOT_USER")
        secret_key = _setting("MINIO_SECRET_KEY", "MINIO_ROOT_PASSWORD")
        if not (endpoint and access_key and secret_key):
            LOGGER.warning("Run artifact storage is set to minio but MinIO is not configured.")
            return None
        from minio import Minio

        retention = _setting("INLUMEN_RUN_ARTIFACT_RETENTION_DAYS")
        return cls(
            Minio(
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=_setting("MINIO_SECURE").lower() in {"1", "true", "yes"},
            ),
            retention_days=int(retention) if retention.isdigit() and int(retention) > 0 else None,
        )

    def _ensure_bucket(self, bucket: str) -> None:
        if bucket in self._ready_buckets:
            return
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)
        if self.retention_days and hasattr(self.client, "set_bucket_lifecycle"):
            from minio.commonconfig import ENABLED, Filter
            from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

            # Run outputs and reusable step results (cache/) expire alike, so
            # results of old code versions do not accumulate.
            self.client.set_bucket_lifecycle(bucket, LifecycleConfig([
                Rule(
                    ENABLED,
                    rule_filter=Filter(prefix=f"{prefix}/"),
                    rule_id=f"expire-{prefix}",
                    expiration=Expiration(days=self.retention_days),
                )
                for prefix in ("runs", "cache")
            ]))
        self._ready_buckets.add(bucket)

    def publish(
        self,
        *,
        workspace_id: str,
        run_id: str,
        outputs: list[tuple[dict[str, Any], Path]],
        receipts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Upload outputs and step receipts; return reference entries in order."""
        bucket = run_artifacts_bucket(workspace_id)
        self._ensure_bucket(bucket)
        references = []
        for entry, path in outputs:
            key = run_output_key(run_id, entry["path"])
            self.client.fput_object(
                bucket, key, str(path),
                content_type=str(entry.get("content_type") or "application/octet-stream"),
            )
            references.append({**entry, "storage": "minio", "bucket": bucket, "key": key})
        for receipt in receipts:
            body = json.dumps(receipt, sort_keys=True).encode("utf-8")
            self.client.put_object(
                bucket, step_receipt_key(run_id, receipt["flow_id"]), io.BytesIO(body), len(body),
                content_type="application/json",
            )
        return references
