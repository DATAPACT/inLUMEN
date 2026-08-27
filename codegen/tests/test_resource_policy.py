import json
import threading
import time
from pathlib import Path

from app.resource_policy import (
    GIB,
    RESOURCE_ADMISSION,
    RESOURCE_PROFILES,
    ResourceAdmissionController,
    host_allocatable_resources,
    profile_allocation,
    select_resource_profile,
)


def _write_requirements(root: Path, content: str) -> None:
    (root / "requirements.txt").write_text(content, encoding="utf-8")


def test_resource_profile_is_inferred_from_reviewed_bundle_metadata(tmp_path):
    _write_requirements(tmp_path, "dagster==1.13.12\n")
    profile, reason = select_resource_profile(tmp_path)
    assert profile.name == "lightweight"
    assert reason == "lightweight runtime dependencies"

    _write_requirements(tmp_path, "dagster==1.13.12\npandas>=2\n")
    profile, _reason = select_resource_profile(tmp_path)
    assert profile.name == "standard"

    (tmp_path / "model-requirements.json").write_text(
        json.dumps(
            {
                "schema_version": "inlumen.model-requirements@1",
                "models": [
                    {
                        "model_id": "openai/whisper-small",
                        "model_revision": "reviewed",
                        "resource_class": "heavy_cpu_or_gpu",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    profile, reason = select_resource_profile(tmp_path)
    assert profile.name == "ml_cpu"
    assert reason == "reviewed local model requirements"


def test_host_capacity_is_reserved_and_profile_is_clamped():
    capacity = host_allocatable_resources({"NCPU": 12, "MemTotal": 8 * GIB})
    allocation = profile_allocation(
        RESOURCE_PROFILES["ml_cpu"],
        capacity,
        reason="test",
    )

    assert capacity["reserved_cpu"] == 3
    assert capacity["allocatable_cpu"] == 9
    assert capacity["reserved_memory_bytes"] >= 2 * GIB
    assert allocation["cpu"] == 4
    assert allocation["memory_bytes"] == 4 * GIB


def test_admission_waits_fifo_until_capacity_is_released():
    controller = ResourceAdmissionController()
    capacity = host_allocatable_resources({"NCPU": 12, "MemTotal": 8 * GIB})
    ml = profile_allocation(
        RESOURCE_PROFILES["ml_cpu"], capacity, reason="first"
    )
    standard = profile_allocation(
        RESOURCE_PROFILES["standard"], capacity, reason="second"
    )
    first = controller.acquire(
        "run-1",
        ml,
        deadline=time.monotonic() + 2,
        cancelled=lambda: False,
        on_wait=lambda _available: None,
    )
    assert first is not None

    waiting = threading.Event()
    acquired: list[dict] = []

    def acquire_second() -> None:
        result = controller.acquire(
            "run-2",
            standard,
            deadline=time.monotonic() + 2,
            cancelled=lambda: False,
            on_wait=lambda _available: waiting.set(),
        )
        if result is not None:
            acquired.append(result)

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert waiting.wait(timeout=1)
    assert acquired == []

    controller.release("run-1")
    thread.join(timeout=1)
    assert acquired and acquired[0]["profile"] == "standard"
    controller.release("run-2")


def test_global_admission_controller_is_available():
    assert isinstance(RESOURCE_ADMISSION, ResourceAdmissionController)
