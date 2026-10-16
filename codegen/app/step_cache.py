"""Reuse step results across pipeline runs.

A step result is reusable when the step's code, its non-secret parameters,
its runtime and the content of its inputs are the same. The key has two
parts so earlier results can be staged before the run starts:

- the code key, computed here from the bundle (step files, its Dagster
  definition, shared runtime files); secret values never enter a bundle;
- the input key, computed by the step itself once its inputs are staged.

Results live in the workspace's run-artifacts bucket under
``cache/<code key>/<input key>/`` and are kept for the newest few input keys
per code key. Steps with side effects, secrets, external connectors or
destinations are never reused.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .run_artifact_names import run_artifacts_bucket

LOGGER = logging.getLogger(__name__)

CacheMode = Literal["reuse", "refresh"]
CACHE_PREFIX = "cache"
KEY_VERSION = b"inlumen.step-cache@1"
DEFS_ROOT = "dagster/src/inlumen_dagster_project/defs"
SHARED_RUNTIME_FILES = (
    "dagster/Dockerfile",
    "dagster/requirements.txt",
    "dagster/constraints.txt",
    "dagster/src/inlumen_dagster_project/components/shell_command.py",
)
FILE_SOURCE_TEMPLATES = {"file", "files", "file upload", "upload"}


def _int_setting(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value.isdigit() else default


@dataclass
class StepCachePlan:
    """Per-step cache keys for one run, by output directory name."""

    steps: dict[str, dict[str, Any]] = field(default_factory=dict)

    def keys_document(self) -> dict[str, Any]:
        return {
            node_dir: {key: step[key] for key in ("flow_id", "static_key", "cacheable", "lookup")}
            for node_dir, step in self.steps.items()
        }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _hash_paths(bundle_root: Path, relative_paths: list[str]) -> str:
    digest = hashlib.sha256(KEY_VERSION + b"\0")
    for relative in sorted(set(relative_paths)):
        path = bundle_root / relative
        if not path.is_file():
            continue
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _files_under(bundle_root: Path, relative_dir: str) -> list[str]:
    root = bundle_root / relative_dir
    if not root.is_dir():
        return []
    return [path.relative_to(bundle_root).as_posix() for path in root.rglob("*") if path.is_file()]


def _cacheable(node: dict[str, Any], manifest_node: dict[str, Any]) -> bool:
    if (manifest_node.get("implementation") or {}).get("cacheable") is False:
        return False
    if node.get("secret_parameters"):
        return False
    capabilities = node.get("capabilities") or {}
    if capabilities.get("side_effects") or capabilities.get("secrets"):
        return False
    kind = str(node.get("kind") or "").lower()
    execution = node.get("execution") or {}
    if kind == "task":
        return execution.get("kind") == "python-package"
    if kind == "source":
        # Uploaded files are hashed into the key; other connectors read
        # external systems that may change between runs.
        return str(node.get("template") or "").strip().lower() in FILE_SOURCE_TEMPLATES
    return False


def _descendants(nodes: list[dict[str, Any]], start: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for node in nodes:
        for parent in node.get("parents") or []:
            children.setdefault(str(parent), []).append(str(node.get("id")))
    seen, pending = {start}, [start]
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in seen:
                seen.add(child)
                pending.append(child)
    return seen


def plan_step_cache(
    bundle_root: Path, *, mode: CacheMode = "reuse", rerun_from: str | None = None
) -> StepCachePlan:
    run_spec = _load_json(bundle_root / "run-spec.json")
    manifest = _load_json(bundle_root / "bundle-manifest.json")
    manifest_nodes = {
        str(item.get("flow_id")): item
        for item in manifest.get("nodes") or []
        if isinstance(item, dict)
    }
    nodes = [node for node in run_spec.get("nodes") or [] if isinstance(node, dict)]
    rerun = _descendants(nodes, str(rerun_from)) if rerun_from else set()
    plan = StepCachePlan()
    for node in nodes:
        flow_id = str(node.get("id") or "")
        output_path = PurePosixPath(str(node.get("output_path") or ""))
        manifest_node = manifest_nodes.get(flow_id, {})
        asset_key = str(manifest_node.get("asset_key") or "")
        if len(output_path.parts) != 2 or not flow_id:
            continue
        node_dir = output_path.parts[1]
        cacheable = bool(asset_key) and _cacheable(node, manifest_node)
        static_key = _hash_paths(bundle_root, [
            *_files_under(bundle_root, f"nodes/{node_dir}"),
            *_files_under(bundle_root, f"inputs/{node_dir}"),
            f"{DEFS_ROOT}/{asset_key}/defs.yaml",
            *SHARED_RUNTIME_FILES,
        ]) if cacheable else ""
        plan.steps[node_dir] = {
            "flow_id": flow_id,
            "static_key": static_key,
            "cacheable": cacheable,
            "lookup": cacheable and mode == "reuse" and flow_id not in rerun,
        }
    return plan


class StepCache:
    def __init__(self, client: Any, workspace_id: str) -> None:
        self.client = client
        self.bucket = run_artifacts_bucket(workspace_id)
        self.keep = max(_int_setting("INLUMEN_STEP_CACHE_KEEP", 3), 1)
        self.max_stage_bytes = _int_setting("INLUMEN_STEP_CACHE_MAX_STAGE_BYTES", 2 * 1024 ** 3)

    def _entries(self, static_key: str) -> list[tuple[str, Any]]:
        """Return (input key, entry.json last-modified) for a code key, newest first."""
        prefix = f"{CACHE_PREFIX}/{static_key}/"
        entries = [
            (item.object_name[len(prefix):].split("/", 1)[0], item.last_modified)
            for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True)
            if item.object_name.endswith("/entry.json")
        ]
        return sorted(entries, key=lambda item: item[1] or 0, reverse=True)

    def stage(self, plan: StepCachePlan, destination: Path) -> int:
        """Download reusable results for steps that may reuse them; return hits staged."""
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "keys.json").write_text(json.dumps(plan.keys_document()), encoding="utf-8")
        if not self.client.bucket_exists(self.bucket):
            return 0
        budget, staged = self.max_stage_bytes, 0
        for node_dir, step in plan.steps.items():
            if not step["lookup"]:
                continue
            for input_key, _modified in self._entries(step["static_key"])[: self.keep]:
                prefix = f"{CACHE_PREFIX}/{step['static_key']}/{input_key}/files/"
                objects = list(self.client.list_objects(self.bucket, prefix=prefix, recursive=True))
                size = sum(int(item.size or 0) for item in objects)
                if size > budget:
                    LOGGER.info("Step cache staging budget reached; skipping %s", node_dir)
                    continue
                target = destination / node_dir / input_key / "files"
                for item in objects:
                    relative = PurePosixPath(item.object_name[len(prefix):])
                    if relative.is_absolute() or ".." in relative.parts:
                        continue
                    self.client.fget_object(self.bucket, item.object_name, str(target / relative))
                budget -= size
                staged += 1
        return staged

    def record(self, plan: StepCachePlan, receipts: list[dict[str, Any]], *, run_id: str) -> int:
        """Store the results of steps that ran (not reused) for later runs."""
        steps_by_flow = {step["flow_id"]: step for step in plan.steps.values()}
        recorded = 0
        for receipt in receipts:
            step = steps_by_flow.get(str(receipt.get("flow_id")))
            published = receipt.get("_published_root")
            input_key = str(receipt.get("input_digest") or "")
            if (
                not step or not step["cacheable"] or receipt.get("cached")
                or receipt.get("status") != "succeeded" or not input_key
                or receipt.get("static_key") != step["static_key"] or not published
            ):
                continue
            prefix = f"{CACHE_PREFIX}/{step['static_key']}/{input_key}/"
            for item in receipt.get("outputs") or []:
                relative = PurePosixPath(str(item.get("path") or ""))
                if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                    continue
                self.client.fput_object(self.bucket, prefix + "files/" + relative.as_posix(), str(Path(published) / relative))
            body = json.dumps({
                "schema_version": "inlumen.step-cache-entry@1",
                "flow_id": step["flow_id"],
                "run_id": run_id,
                "created_at": time.time(),
                "outputs": receipt.get("outputs") or [],
            }).encode("utf-8")
            self.client.put_object(self.bucket, prefix + "entry.json", io.BytesIO(body), len(body),
                                   content_type="application/json")
            self.prune(step["static_key"])
            recorded += 1
        return recorded

    def prune(self, static_key: str) -> None:
        from minio.deleteobjects import DeleteObject

        for input_key, _modified in self._entries(static_key)[self.keep:]:
            prefix = f"{CACHE_PREFIX}/{static_key}/{input_key}/"
            names = [item.object_name for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True)]
            list(self.client.remove_objects(self.bucket, (DeleteObject(name) for name in names)))
