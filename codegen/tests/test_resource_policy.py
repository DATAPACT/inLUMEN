import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

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


def test_twenty_vm_jobs_share_bounded_execution_budget(monkeypatch):
    monkeypatch.setenv("CODEGEN_EXECUTION_CPU_BUDGET", "4")
    monkeypatch.setenv("CODEGEN_EXECUTION_MEMORY_GIB", "8")
    monkeypatch.setenv("CODEGEN_ML_CPU_THREADS", "2")
    monkeypatch.setenv("CODEGEN_EXECUTION_MAX_ACTIVE_RUNS", "2")
    capacity = host_allocatable_resources({"NCPU": 8, "MemTotal": 30 * GIB})
    allocation = profile_allocation(RESOURCE_PROFILES["ml_cpu"], capacity, reason="VM session")
    assert allocation["cpu"] == 2 and allocation["memory_bytes"] == 4 * GIB
    controller = ResourceAdmissionController()
    barrier = threading.Barrier(20)
    lock = threading.Lock()
    active = 0
    peak = 0
    waits = []

    def execute(index):
        nonlocal active, peak
        barrier.wait(timeout=5)
        admitted = controller.acquire(str(index), allocation, deadline=time.monotonic() + 5,
            cancelled=lambda: False, on_wait=lambda available: waits.append(available))
        assert admitted is not None
        try:
            with lock:
                active += 1
                peak = max(peak, active)
                assert active * admitted["cpu"] <= 4
                assert active * admitted["memory_bytes"] <= 8 * GIB
            time.sleep(.01)
        finally:
            with lock:
                active -= 1
            controller.release(str(index))
        return index

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert sorted(pool.map(execute, range(20))) == list(range(20))
    assert peak == 2 and waits
    assert not controller._active and not controller._waiting


def test_active_run_limit_applies_even_when_small_jobs_fit():
    controller = ResourceAdmissionController(max_active_runs=1)
    capacity = host_allocatable_resources({"NCPU": 8, "MemTotal": 30 * GIB})
    allocation = profile_allocation(RESOURCE_PROFILES["lightweight"], capacity, reason="small")
    assert controller.acquire("first", allocation, deadline=time.monotonic() + 1,
        cancelled=lambda: False, on_wait=lambda available: None)
    assert controller.acquire("cancelled", allocation, deadline=time.monotonic() + 1,
        cancelled=lambda: True, on_wait=lambda available: None) is None
    assert controller.acquire("expired", allocation, deadline=time.monotonic() + .01,
        cancelled=lambda: False, on_wait=lambda available: None) is None
    assert controller._waiting == []
    controller.release("first")
    assert controller.acquire("next", allocation, deadline=time.monotonic() + 1,
        cancelled=lambda: False, on_wait=lambda available: None)
    controller.release("next")


@pytest.mark.parametrize("name", ["CODEGEN_EXECUTION_CPU_BUDGET", "CODEGEN_EXECUTION_MEMORY_GIB", "CODEGEN_EXECUTION_MAX_ACTIVE_RUNS", "CODEGEN_ML_CPU_THREADS"])
@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_bad_execution_settings_fail_closed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        controller = ResourceAdmissionController()
        capacity = host_allocatable_resources({"NCPU": 8, "MemTotal": 30 * GIB})
        profile_allocation(RESOURCE_PROFILES["ml_cpu"], capacity, reason="invalid")
