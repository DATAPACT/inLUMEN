from __future__ import annotations

import base64
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")


class PipelineArtifactStore:
    """Filesystem payload store; lifecycle metadata remains in SQLite."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def store_bundle(
        self,
        run_id: str,
        files: list[dict[str, Any]],
        workspace_id: str = "local-workspace",
    ) -> str:
        path = self._run_root(run_id, workspace_id) / "bundle-files.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(files, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return str(path)

    def load_bundle(self, reference: str) -> list[dict[str, Any]]:
        path = self._safe_reference(reference)
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, list):
            raise TypeError("Stored executable bundle is not a file list.")
        return [deepcopy(item) for item in parsed if isinstance(item, dict)]

    def store_outputs(
        self,
        run_id: str,
        outputs: list[dict[str, Any]],
        workspace_id: str = "local-workspace",
    ) -> list[dict[str, Any]]:
        stored = []
        output_root = self._run_root(run_id, workspace_id) / "artifacts"
        for entry in outputs:
            path = self._safe_relative(entry.get("path"))
            destination = (output_root / Path(*path.parts)).resolve()
            if output_root.resolve() not in destination.parents:
                raise ValueError(f"Unsafe pipeline output path: {path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = str(entry.get("content") or "")
            body = (
                base64.b64decode(content, validate=True)
                if str(entry.get("content_encoding") or "") == "base64"
                else content.encode("utf-8")
            )
            destination.write_bytes(body)
            metadata = {
                key: deepcopy(value)
                for key, value in entry.items()
                if key not in {"content", "content_encoding"}
            }
            metadata["_storage_path"] = str(destination)
            stored.append(metadata)
        return stored

    def read_output(self, reference: str) -> bytes:
        return self._safe_reference(reference).read_bytes()

    def clear(self, workspace_id: str | None = None) -> int:
        """Remove run payloads, optionally constrained to one workspace."""
        removed = 0
        target = (
            self.root if workspace_id is None else self._workspace_root(workspace_id)
        )
        if not target.exists():
            return 0
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        if workspace_id is not None:
            target.rmdir()
        return removed

    def _workspace_root(self, workspace_id: str) -> Path:
        if not SAFE_RUN_ID.fullmatch(workspace_id):
            raise ValueError("Unsafe workspace id.")
        return self.root / workspace_id

    def _run_root(self, run_id: str, workspace_id: str = "local-workspace") -> Path:
        if not SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("Unsafe pipeline run id.")
        return self._workspace_root(workspace_id) / run_id

    def _safe_reference(self, reference: str) -> Path:
        path = Path(str(reference or "")).expanduser().resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("Artifact reference escapes the runner artifact store.")
        return path

    @staticmethod
    def _safe_relative(value: Any) -> PurePosixPath:
        path = PurePosixPath(str(value or "").strip())
        if not str(path) or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe pipeline artifact path: {value!r}")
        return path
