from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIB = 1024**3
MIB = 1024**2


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    cpu: int
    memory_bytes: int
    description: str


RESOURCE_PROFILES = {
    "lightweight": ResourceProfile(
        name="lightweight",
        cpu=1,
        memory_bytes=1 * GIB,
        description="Lightweight Python pipeline",
    ),
    "standard": ResourceProfile(
        name="standard",
        cpu=2,
        memory_bytes=2 * GIB,
        description="Standard data-processing pipeline",
    ),
    "ml_cpu": ResourceProfile(
        name="ml_cpu",
        cpu=4,
        memory_bytes=4 * GIB,
        description="CPU model inference pipeline",
    ),
}

_ML_RESOURCE_CLASSES = {"gpu_preferred", "heavy_cpu_or_gpu"}
_STANDARD_RESOURCE_CLASSES = {"lightweight_cpu"}
_ML_REQUIREMENT_MARKERS = (
    "torch",
    "tensorflow",
    "transformers",
    "faster-whisper",
    "ctranslate2",
    "onnxruntime",
)
_STANDARD_REQUIREMENT_MARKERS = (
    "numpy",
    "pandas",
    "polars",
    "pyarrow",
    "scikit-learn",
    "scipy",
)


def _requirements_text(project_root: Path) -> str:
    path = project_root / "requirements.txt"
    return path.read_text(encoding="utf-8").lower() if path.is_file() else ""


def _model_requirements(project_root: Path) -> list[dict[str, Any]]:
    path = project_root / "model-requirements.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    return [item for item in payload.get("models") or [] if isinstance(item, dict)]


def select_resource_profile(project_root: Path) -> tuple[ResourceProfile, str]:
    """Select a platform-owned profile from reviewed bundle metadata."""
    models = _model_requirements(project_root)
    resource_classes = {
        str(model.get("resource_class") or "").strip().lower()
        for model in models
    }
    if models and (
        resource_classes & _ML_RESOURCE_CLASSES
        or not resource_classes
        or "" in resource_classes
    ):
        return RESOURCE_PROFILES["ml_cpu"], "reviewed local model requirements"
    if resource_classes & _STANDARD_RESOURCE_CLASSES:
        return RESOURCE_PROFILES["standard"], "lightweight local model requirements"

    requirements = _requirements_text(project_root)
    if any(marker in requirements for marker in _ML_REQUIREMENT_MARKERS):
        return RESOURCE_PROFILES["ml_cpu"], "CPU model framework dependencies"
    if any(marker in requirements for marker in _STANDARD_REQUIREMENT_MARKERS):
        return RESOURCE_PROFILES["standard"], "data-processing dependencies"
    return RESOURCE_PROFILES["lightweight"], "lightweight runtime dependencies"


def host_allocatable_resources(docker_info: dict[str, Any]) -> dict[str, int]:
    """Reserve capacity for Docker and the inLUMEN control-plane services."""
    total_cpu = max(int(docker_info.get("NCPU") or 1), 1)
    total_memory = max(int(docker_info.get("MemTotal") or GIB), 512 * MIB)
    reserved_cpu = min(
        max(total_cpu - 1, 0),
        max(2, math.ceil(total_cpu * 0.20)),
    )
    reserved_memory = min(
        max(total_memory - 512 * MIB, 0),
        max(2 * GIB, math.ceil(total_memory * 0.30)),
    )
    return {
        "host_cpu": total_cpu,
        "host_memory_bytes": total_memory,
        "reserved_cpu": reserved_cpu,
        "reserved_memory_bytes": reserved_memory,
        "allocatable_cpu": max(total_cpu - reserved_cpu, 1),
        "allocatable_memory_bytes": max(
            total_memory - reserved_memory,
            512 * MIB,
        ),
    }


def profile_allocation(
    profile: ResourceProfile,
    capacity: dict[str, int],
    *,
    reason: str,
) -> dict[str, Any]:
    cpu = min(profile.cpu, capacity["allocatable_cpu"])
    memory_bytes = min(
        profile.memory_bytes,
        capacity["allocatable_memory_bytes"],
    )
    return {
        "profile": profile.name,
        "description": profile.description,
        "reason": reason,
        "cpu": max(cpu, 1),
        "memory_bytes": max(memory_bytes, 512 * MIB),
        **capacity,
    }


class ResourceAdmissionController:
    """FIFO admission control for one execution-worker process."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._active: dict[str, dict[str, Any]] = {}
        self._waiting: list[str] = []

    def acquire(
        self,
        execution_id: str,
        allocation: dict[str, Any],
        *,
        deadline: float,
        cancelled: Callable[[], bool],
        on_wait: Callable[[dict[str, int]], None],
    ) -> dict[str, Any] | None:
        with self._condition:
            if execution_id in self._active:
                return dict(self._active[execution_id])
            if execution_id not in self._waiting:
                self._waiting.append(execution_id)

        while True:
            if cancelled() or time.monotonic() >= deadline:
                self._remove_waiter(execution_id)
                return None
            with self._condition:
                used_cpu = sum(int(item["cpu"]) for item in self._active.values())
                used_memory = sum(
                    int(item["memory_bytes"]) for item in self._active.values()
                )
                available = {
                    "cpu": max(int(allocation["allocatable_cpu"]) - used_cpu, 0),
                    "memory_bytes": max(
                        int(allocation["allocatable_memory_bytes"]) - used_memory,
                        0,
                    ),
                    "queue_position": self._waiting.index(execution_id) + 1,
                }
                is_next = self._waiting[0] == execution_id
                fits = (
                    int(allocation["cpu"]) <= available["cpu"]
                    and int(allocation["memory_bytes"]) <= available["memory_bytes"]
                )
                if is_next and fits:
                    self._waiting.pop(0)
                    self._active[execution_id] = dict(allocation)
                    return dict(allocation)
            on_wait(available)
            with self._condition:
                self._condition.wait(timeout=min(max(deadline - time.monotonic(), 0), 1))

    def release(self, execution_id: str) -> None:
        with self._condition:
            self._active.pop(execution_id, None)
            if execution_id in self._waiting:
                self._waiting.remove(execution_id)
            self._condition.notify_all()

    def _remove_waiter(self, execution_id: str) -> None:
        with self._condition:
            if execution_id in self._waiting:
                self._waiting.remove(execution_id)
            self._condition.notify_all()


RESOURCE_ADMISSION = ResourceAdmissionController()
