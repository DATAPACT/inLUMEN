from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException
from requests.exceptions import ReadTimeout

try:
    import yaml
except ImportError:  # pragma: no cover - service image installs PyYAML.
    yaml = None


SUPPORTED_BUNDLE_MANIFEST_VERSIONS = frozenset(
    {"inlumen.deployment-bundle@1", "inlumen.deployment-bundle@2"}
)
SUPPORTED_RUN_SPEC_VERSIONS = frozenset(
    {"inlumen.run-spec@1", "inlumen.run-spec@2", "inlumen.run-spec@3"}
)

_ACTIVE_DEPLOYMENT_PROCESSES: dict[str, subprocess.Popen[str]] = {}
_CANCELLED_DEPLOYMENT_EXECUTIONS: set[str] = set()
_DEPLOYMENT_PROCESS_LOCK = threading.RLock()


def prepare_deployment_execution(execution_id: str) -> None:
    """Register a fresh execution id before dispatching validation work."""
    if not execution_id:
        return
    with _DEPLOYMENT_PROCESS_LOCK:
        _CANCELLED_DEPLOYMENT_EXECUTIONS.discard(execution_id)


def cancel_deployment_execution(execution_id: str) -> None:
    """Cancel the active Dagster/install subprocess for an execution."""
    if not execution_id:
        return
    with _DEPLOYMENT_PROCESS_LOCK:
        _CANCELLED_DEPLOYMENT_EXECUTIONS.add(execution_id)
        process = _ACTIVE_DEPLOYMENT_PROCESSES.get(execution_id)
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _deployment_execution_cancelled(execution_id: str | None) -> bool:
    if not execution_id:
        return False
    with _DEPLOYMENT_PROCESS_LOCK:
        return execution_id in _CANCELLED_DEPLOYMENT_EXECUTIONS


def _dagster_project_root(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if (candidate / "pyproject.toml").is_file() and (candidate / "src").is_dir():
        return candidate
    child = candidate / "dagster_project"
    if (child / "pyproject.toml").is_file() and (child / "src").is_dir():
        return child.resolve()
    canonical_child = candidate / "dagster"
    if (canonical_child / "pyproject.toml").is_file() and (
        canonical_child / "src"
    ).is_dir():
        return canonical_child.resolve()
    raise FileNotFoundError(
        f"Could not find a generated dagster_project under {candidate}"
    )


def _bundle_root(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if (candidate / "bundle-manifest.json").is_file():
        return candidate
    if (
        candidate.name in {"dagster", "dagster_project"}
        and (candidate.parent / "bundle-manifest.json").is_file()
    ):
        return candidate.parent
    if candidate.suffix.lower() in {".yaml", ".yml"}:
        return candidate.parent
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    if _deployment_execution_cancelled(execution_id):
        return {
            "command": command,
            "returncode": None,
            "output": "Execution cancelled before command launch.",
            "ok": False,
            "cancelled": True,
        }
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if execution_id:
            with _DEPLOYMENT_PROCESS_LOCK:
                if execution_id in _CANCELLED_DEPLOYMENT_EXECUTIONS:
                    process.terminate()
                _ACTIVE_DEPLOYMENT_PROCESSES[execution_id] = process
        try:
            output, _stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _stderr = process.communicate()
            return {
                "command": command,
                "returncode": None,
                "output": f"Command timed out after {timeout_seconds}s.\n{output}",
                "ok": False,
            }
        cancelled = _deployment_execution_cancelled(execution_id)
        return {
            "command": command,
            "returncode": process.returncode,
            "output": output,
            "ok": process.returncode == 0 and not cancelled,
            **({"cancelled": True} if cancelled else {}),
        }
    except OSError as exc:
        return {
            "command": command,
            "returncode": None,
            "output": str(exc),
            "ok": False,
        }
    finally:
        if execution_id:
            with _DEPLOYMENT_PROCESS_LOCK:
                active = _ACTIVE_DEPLOYMENT_PROCESSES.get(execution_id)
                if active is locals().get("process"):
                    _ACTIVE_DEPLOYMENT_PROCESSES.pop(execution_id, None)


def _ensure_venv(
    project_root: Path,
    *,
    reinstall: bool,
    timeout_seconds: int,
    execution_id: str | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    venv_dir = project_root / ".inlumen_dagster_validation_venv"
    steps: list[dict[str, Any]] = []
    uv = shutil.which("uv")
    if reinstall and venv_dir.exists():
        shutil.rmtree(venv_dir)
    if not venv_dir.exists():
        create_command = (
            [uv, "venv", "--python", sys.executable, str(venv_dir)]
            if uv
            else [sys.executable, "-m", "venv", str(venv_dir)]
        )
        steps.append(
            _run(
                create_command,
                cwd=project_root,
                timeout_seconds=timeout_seconds,
                execution_id=execution_id,
            )
        )
        if not steps[-1]["ok"]:
            return venv_dir, steps

    python = venv_dir / "bin" / "python"
    install_command = (
        [uv, "pip", "install", "--python", str(python), "-e", "."]
        if uv
        else [str(python), "-m", "pip", "install", "-e", "."]
    )
    steps.append(
        _run(
            install_command,
            cwd=project_root,
            timeout_seconds=timeout_seconds,
            execution_id=execution_id,
        )
    )
    return venv_dir, steps


def _validation_script() -> str:
    return r"""
import json
import sys

import dagster as dg
from inlumen_dagster_project.definitions import defs

definitions = defs() if callable(defs) else defs
asset_keys = sorted(str(key.to_user_string()) for key in definitions.resolve_asset_graph().get_all_asset_keys())
print(json.dumps({"asset_keys": asset_keys}, indent=2))
"""


def _materialize_script() -> str:
    return r"""
import dagster as dg
from inlumen_dagster_project.definitions import defs

definitions = defs() if callable(defs) else defs
assets = list(definitions.assets or [])
resources = dict(definitions.resources or {})
result = dg.materialize(assets, resources=resources, raise_on_error=False)
if not result.success:
    raise SystemExit(1)
"""


def _isolated_dagster_execution(
    project_root: Path,
    *,
    execution_id: str,
    timeout_seconds: int,
    runtime_secrets: dict[str, str] | None,
) -> dict[str, Any]:
    """Build and materialize the generated project outside the control plane."""
    bundle_root = _bundle_root(project_root)
    dockerfile = project_root / "Dockerfile"
    execution_dockerfile = project_root / "Dockerfile.inlumen-run"
    # BuildKit cache mounts only optimize the exported image build. The Docker
    # SDK can use the legacy builder, so native execution derives an equivalent
    # no-cache Dockerfile without changing dependencies or runtime source.
    execution_dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace(
            "RUN --mount=type=cache,target=/root/.cache/uv ", "RUN "
        ),
        encoding="utf-8",
    )
    dockerfile_relative = execution_dockerfile.relative_to(bundle_root).as_posix()
    workspace_dir = bundle_root / "workspaces"
    output_dir = bundle_root / "outputs"
    input_dir = bundle_root / "inputs"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.chmod(0o777)
    output_dir.chmod(0o777)
    report: dict[str, Any] = {
        "project_root": str(project_root),
        "package_manager": "container-image",
        "execution_isolation": "docker",
        "ok": False,
        "steps": [],
    }
    client = None
    container = None
    deadline = time.monotonic() + timeout_seconds
    try:
        if _deployment_execution_cancelled(execution_id):
            report["errors"] = ["Dagster execution was cancelled."]
            return report
        client = docker.from_env()
        snapshot_digest = hashlib.sha256()
        for snapshot_file in sorted(
            item for item in bundle_root.rglob("*") if item.is_file()
        ):
            relative = snapshot_file.relative_to(bundle_root)
            if relative.parts[0] in {"outputs", "workspaces"}:
                continue
            if snapshot_file == execution_dockerfile:
                continue
            snapshot_digest.update(relative.as_posix().encode("utf-8"))
            snapshot_digest.update(b"\0")
            snapshot_digest.update(snapshot_file.read_bytes())
            snapshot_digest.update(b"\0")
        snapshot_hash = snapshot_digest.hexdigest()[:20]
        image_tag = f"inlumen-dagster-run:{snapshot_hash}"
        image, build_logs = client.images.build(
            path=str(bundle_root),
            dockerfile=dockerfile_relative,
            tag=image_tag,
            rm=True,
            forcerm=True,
            labels={"inlumen.pipeline.snapshot": snapshot_hash},
        )
        build_output = "\n".join(
            str(item.get("stream") or item.get("error") or "").rstrip()
            for item in build_logs
            if isinstance(item, dict)
            and str(item.get("stream") or item.get("error") or "").strip()
        )
        report["steps"].append(
            {
                "name": "image_build",
                "command": ["docker", "build", "-f", dockerfile_relative, "."],
                "returncode": 0,
                "output": build_output[-12000:],
                "ok": True,
            }
        )
        if _deployment_execution_cancelled(execution_id):
            report["errors"] = ["Dagster execution was cancelled."]
            return report
        model_requirements = project_root / "model-requirements.json"
        model_prefetch = project_root / "model_prefetch.py"
        model_volume = os.getenv(
            "INLUMEN_MODEL_STORE_VOLUME", "inlumen_model_store"
        ).strip()
        has_models = model_requirements.is_file() and model_prefetch.is_file()
        if has_models:
            prefetch_environment = {
                **(runtime_secrets or {}),
                "HF_HOME": "/models/huggingface",
                "HF_HUB_CACHE": "/models/huggingface",
                "HF_HUB_DISABLE_XET": os.getenv("HF_HUB_DISABLE_XET", "1"),
                "HF_HUB_ETAG_TIMEOUT": os.getenv("HF_HUB_ETAG_TIMEOUT", "30"),
                "HF_HUB_DOWNLOAD_TIMEOUT": os.getenv(
                    "HF_HUB_DOWNLOAD_TIMEOUT", "600"
                ),
                "HF_HUB_OFFLINE": "0",
                "TRANSFORMERS_OFFLINE": "0",
                "INLUMEN_MODEL_ROOT": "/models",
                "INLUMEN_MODEL_REQUIREMENTS": (
                    "/workspace/dagster/model-requirements.json"
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }
            container = client.containers.run(
                image.id,
                command=["python", "/workspace/dagster/model_prefetch.py"],
                working_dir="/workspace/dagster",
                detach=True,
                remove=False,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                pids_limit=256,
                mem_limit=os.getenv("CODEGEN_VALIDATION_MEMORY_LIMIT", "1g"),
                nano_cpus=int(
                    float(os.getenv("CODEGEN_VALIDATION_CPU_LIMIT", "1"))
                    * 1_000_000_000
                ),
                tmpfs={"/tmp": "rw,noexec,nosuid,size=256m"},
                environment=prefetch_environment,
                volumes={
                    model_volume: {"bind": "/models", "mode": "rw"},
                },
                stdout=True,
                stderr=True,
                labels={"inlumen.codegen.run_id": execution_id},
            )
            try:
                prefetch_result = container.wait(
                    timeout=max(int(deadline - time.monotonic()), 1)
                )
            except ReadTimeout:
                container.kill()
                report["errors"] = ["Reviewed model prefetch timed out."]
                return report
            prefetch_logs = container.logs(stdout=True, stderr=True).decode(
                "utf-8", errors="replace"
            )
            prefetch_status = int(prefetch_result.get("StatusCode", 1))
            report["steps"].append(
                {
                    "name": "model_prefetch",
                    "command": [
                        "python",
                        "/workspace/dagster/model_prefetch.py",
                    ],
                    "returncode": prefetch_status,
                    "output": prefetch_logs[-12000:],
                    "ok": prefetch_status == 0,
                }
            )
            container.remove(force=True)
            container = None
            if prefetch_status != 0:
                report["errors"] = ["Reviewed model prefetch failed."]
                return report
        environment = {
            **(runtime_secrets or {}),
            "DAGSTER_HOME": "/tmp/dagster-home",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            **(
                {
                    "HF_HOME": "/models/huggingface",
                    "HF_HUB_CACHE": "/models/huggingface",
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "INLUMEN_MODEL_ROOT": "/models",
                }
                if has_models
                else {}
            ),
        }
        execution_volumes = {
            str(input_dir.resolve()): {
                "bind": "/workspace/inputs",
                "mode": "ro",
            },
            str(output_dir.resolve()): {
                "bind": "/workspace/outputs",
                "mode": "rw",
            },
            str(workspace_dir.resolve()): {
                "bind": "/workspace/workspaces",
                "mode": "rw",
            },
            **(
                {model_volume: {"bind": "/models", "mode": "ro"}}
                if has_models
                else {}
            ),
        }
        container = client.containers.run(
            image.id,
            command=["python", "-c", _materialize_script()],
            working_dir="/workspace/dagster",
            user="65532:65532",
            detach=True,
            remove=False,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=256,
            mem_limit=os.getenv("CODEGEN_VALIDATION_MEMORY_LIMIT", "1g"),
            nano_cpus=int(
                float(os.getenv("CODEGEN_VALIDATION_CPU_LIMIT", "1"))
                * 1_000_000_000
            ),
            tmpfs={"/tmp": "rw,noexec,nosuid,size=256m,mode=1777"},
            environment=environment,
            volumes=execution_volumes,
            stdout=True,
            stderr=True,
            labels={"inlumen.codegen.run_id": execution_id},
        )
        try:
            result = container.wait(timeout=max(int(deadline - time.monotonic()), 1))
        except ReadTimeout:
            container.kill()
            report["steps"].append(
                {
                    "name": "dagster_materialize",
                    "command": ["python", "-c", "<dagster materialize>"],
                    "returncode": None,
                    "output": f"Dagster execution timed out after {timeout_seconds}s.",
                    "ok": False,
                }
            )
            report["errors"] = ["Dagster asset materialization timed out."]
            return report
        logs = container.logs(stdout=True, stderr=True).decode(
            "utf-8", errors="replace"
        )
        status_code = int(result.get("StatusCode", 1))
        cancelled = _deployment_execution_cancelled(execution_id)
        report["steps"].append(
            {
                "name": "dagster_materialize",
                "command": ["python", "-c", "<dagster materialize>"],
                "returncode": status_code,
                "output": logs[-12000:],
                "ok": status_code == 0 and not cancelled,
                **({"cancelled": True} if cancelled else {}),
            }
        )
        if cancelled:
            report["errors"] = ["Dagster execution was cancelled."]
            return report
        if status_code != 0:
            report["errors"] = ["Dagster asset materialization failed."]
            return report
        report["ok"] = True
        return report
    except (DockerException, OSError, ValueError) as exc:
        report["errors"] = [f"Isolated Dagster execution failed: {exc}"]
        return report
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except DockerException:
                pass
        if client is not None:
            client.close()


def validate_dagster_project(
    path: Path,
    *,
    materialize: bool = True,
    reinstall: bool = False,
    skip_install: bool = False,
    timeout_seconds: int = 900,
    runtime_secrets: dict[str, str] | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    project_root = _dagster_project_root(path)
    if execution_id:
        return _isolated_dagster_execution(
            project_root,
            execution_id=execution_id,
            timeout_seconds=timeout_seconds,
            runtime_secrets=runtime_secrets,
        )
    report: dict[str, Any] = {
        "project_root": str(project_root),
        "package_manager": "uv" if shutil.which("uv") else "pip-fallback",
        "ok": False,
        "steps": [],
    }

    required_files = [
        "pyproject.toml",
        "src/inlumen_dagster_project/definitions.py",
        "src/inlumen_dagster_project/components/shell_command.py",
    ]
    missing = [item for item in required_files if not (project_root / item).is_file()]
    if missing:
        report["errors"] = [f"Missing required file: {item}" for item in missing]
        return report

    dockerfile = project_root / "Dockerfile"
    pyproject_content = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile_content = (
        dockerfile.read_text(encoding="utf-8") if dockerfile.is_file() else ""
    )
    if (
        '"dagster", "dev"' in dockerfile_content
        and "dagster-webserver" not in pyproject_content
    ):
        report["errors"] = [
            (
                "Generated Dagster Dockerfile runs `dagster dev`, but "
                "pyproject.toml does not install dagster-webserver."
            )
        ]
        return report

    venv_dir = project_root / ".inlumen_dagster_validation_venv"
    if skip_install:
        if not (venv_dir / "bin" / "python").is_file():
            report["errors"] = [
                (
                    "skip_install was requested, but no validation venv exists. "
                    "Run once without --skip-install."
                )
            ]
            return report
    else:
        venv_dir, install_steps = _ensure_venv(
            project_root,
            reinstall=reinstall,
            timeout_seconds=timeout_seconds,
            execution_id=execution_id,
        )
        report["steps"].extend(install_steps)
        if any(not step["ok"] for step in install_steps):
            report["errors"] = ["Generated Dagster project installation failed."]
            return report

    python = venv_dir / "bin" / "python"
    env = {
        **os.environ,
        **(runtime_secrets or {}),
        "DAGSTER_HOME": str(project_root / ".dagster_home"),
        "PYTHONPATH": str(project_root / "src"),
    }
    Path(env["DAGSTER_HOME"]).mkdir(parents=True, exist_ok=True)

    model_prefetch_script = project_root / "model-prefetch.py"
    model_requirements_path = project_root / "model-requirements.json"
    if model_prefetch_script.is_file() and model_requirements_path.is_file():
        model_root = Path(
            os.getenv("INLUMEN_MODEL_ROOT") or project_root / ".inlumen-models"
        ).resolve()
        model_root.mkdir(parents=True, exist_ok=True)
        prefetch_env = {
            **env,
            "HF_HOME": str(model_root / "huggingface"),
            "HF_HUB_CACHE": str(model_root / "huggingface"),
            "HF_HUB_DISABLE_XET": os.getenv("HF_HUB_DISABLE_XET", "1"),
            "HF_HUB_ETAG_TIMEOUT": os.getenv("HF_HUB_ETAG_TIMEOUT", "30"),
            "HF_HUB_DOWNLOAD_TIMEOUT": os.getenv("HF_HUB_DOWNLOAD_TIMEOUT", "600"),
            "HF_HUB_OFFLINE": "0",
            "TRANSFORMERS_OFFLINE": "0",
            "INLUMEN_ACCELERATOR": os.getenv("INLUMEN_ACCELERATOR", "cpu"),
            "INLUMEN_ASR_DEVICE": os.getenv("INLUMEN_ASR_DEVICE", "auto"),
            "INLUMEN_ASR_PROFILE": os.getenv("INLUMEN_ASR_PROFILE", "auto"),
            "INLUMEN_MODEL_ROOT": str(model_root),
            "INLUMEN_MODEL_REQUIREMENTS": str(model_requirements_path),
            "INLUMEN_MODEL_VERIFY_ON_START": os.getenv(
                "INLUMEN_MODEL_VERIFY_ON_START", "manifest"
            ),
        }
        prefetch_step = _run(
            [str(python), str(model_prefetch_script)],
            cwd=project_root,
            timeout_seconds=timeout_seconds,
            env=prefetch_env,
            execution_id=execution_id,
        )
        prefetch_step["name"] = "model_prefetch"
        report["steps"].append(prefetch_step)
        if not prefetch_step["ok"]:
            report["errors"] = ["Reviewed model prefetch or verification failed."]
            return report
        env.update(
            {
                "HF_HOME": prefetch_env["HF_HOME"],
                "HF_HUB_CACHE": prefetch_env["HF_HUB_CACHE"],
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "INLUMEN_ACCELERATOR": prefetch_env["INLUMEN_ACCELERATOR"],
                "INLUMEN_ASR_DEVICE": prefetch_env["INLUMEN_ASR_DEVICE"],
                "INLUMEN_ASR_PROFILE": prefetch_env["INLUMEN_ASR_PROFILE"],
                "INLUMEN_MODEL_ROOT": prefetch_env["INLUMEN_MODEL_ROOT"],
            }
        )

    load_step = _run(
        [str(python), "-c", _validation_script()],
        cwd=project_root,
        timeout_seconds=timeout_seconds,
        env=env,
        execution_id=execution_id,
    )
    report["steps"].append(load_step)
    if not load_step["ok"]:
        report["errors"] = ["Dagster definitions failed to load."]
        return report

    try:
        report["assets"] = json.loads(load_step["output"]).get("asset_keys", [])
    except (AttributeError, json.JSONDecodeError, TypeError):
        report["assets"] = []

    if materialize:
        materialize_step = _run(
            [str(python), "-c", _materialize_script()],
            cwd=project_root,
            timeout_seconds=timeout_seconds,
            env=env,
            execution_id=execution_id,
        )
        report["steps"].append(materialize_step)
        if not materialize_step["ok"]:
            report["errors"] = ["Dagster asset materialization failed."]
            return report

    report["ok"] = True
    return report


def _load_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


TABLE_FORMATS = {"csv", "tsv", "parquet", "xlsx", "xls", "arrow", "feather"}
JSON_FORMATS = {"json", "jsonl", "ndjson"}
TEXT_FORMATS = {
    "txt",
    "md",
    "markdown",
    "xml",
    "yaml",
    "yml",
    "html",
    "htm",
    "rtf",
    "log",
}
IMAGE_FORMATS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "bmp",
    "tif",
    "tiff",
    "svg",
}
AUDIO_FORMATS = {"aac", "flac", "m4a", "mp3", "ogg", "opus", "wav"}
VIDEO_FORMATS = {"avi", "mkv", "mov", "mp4", "mpeg", "mpg", "webm"}
DOCUMENT_FORMATS = {"doc", "docx", "odp", "odt", "pdf", "ppt", "pptx"}
CANONICAL_ARTIFACT_KINDS = {
    "table",
    "json",
    "text",
    "image",
    "audio",
    "video",
    "document",
    "model",
    "directory",
    "binary",
}


def _canonical_artifact(
    filename: str,
    *,
    kind: Any = "",
    file_format: Any = "",
) -> dict[str, str]:
    normalized_format = str(file_format or "").strip().lower().lstrip(".")
    if not normalized_format:
        normalized_format = Path(filename).suffix.lower().lstrip(".") or "binary"
    if normalized_format in TABLE_FORMATS:
        inferred_kind = "table"
    elif normalized_format in JSON_FORMATS:
        inferred_kind = "json"
    elif normalized_format in TEXT_FORMATS:
        inferred_kind = "text"
    elif normalized_format in IMAGE_FORMATS:
        inferred_kind = "image"
    elif normalized_format in AUDIO_FORMATS:
        inferred_kind = "audio"
    elif normalized_format in VIDEO_FORMATS:
        inferred_kind = "video"
    elif normalized_format in DOCUMENT_FORMATS:
        inferred_kind = "document"
    else:
        inferred_kind = "binary"
    declared_kind = str(kind or "").strip().lower()
    return {
        "kind": (
            declared_kind
            if declared_kind in CANONICAL_ARTIFACT_KINDS
            else inferred_kind
        ),
        "format": normalized_format,
    }


def _root_contract_descriptors(
    bundle_root: Path,
    bundle_manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    descriptors: dict[str, dict[str, Any]] = {}
    nodes = (
        bundle_manifest.get("nodes")
        if isinstance(bundle_manifest.get("nodes"), list)
        else []
    )
    for node in nodes:
        if not isinstance(node, dict) or node.get("parents"):
            continue
        node_path = str(node.get("path") or "").strip()
        if not node_path:
            continue
        node_manifest = _load_json(bundle_root / node_path / "node-manifest.json")
        data_contract = (
            node_manifest.get("data_contract")
            if isinstance(node_manifest.get("data_contract"), dict)
            else {}
        )
        inputs = (
            data_contract.get("inputs")
            if isinstance(data_contract.get("inputs"), list)
            else []
        )
        for descriptor in inputs:
            if not isinstance(descriptor, dict):
                continue
            filename = str(
                descriptor.get("filename") or descriptor.get("name") or ""
            ).strip()
            if filename:
                descriptors[filename] = descriptor
    return descriptors


def _input_contract_errors(
    bundle_root: Path,
    bundle_manifest: dict[str, Any],
) -> list[str]:
    manifest_path = bundle_root / "inputs" / "input_manifest.json"
    if not manifest_path.is_file():
        # Filesystem-first bundles do not expose an input manifest.  The only
        # deployment-level check is that any persisted root fixtures exist.
        descriptors = _root_contract_descriptors(bundle_root, bundle_manifest)
        available = {
            path.relative_to(bundle_root / "inputs").as_posix()
            for path in (bundle_root / "inputs").rglob("*")
            if path.is_file()
        }
        available_filenames = {Path(path).name for path in available}
        return [
            f"Root node input {filename} is missing from inputs/."
            for filename in sorted(descriptors)
            if filename not in available_filenames
        ]
    input_manifest = _load_json(manifest_path)
    raw_inputs = input_manifest.get("inputs")
    if not isinstance(raw_inputs, list):
        return ["inputs/input_manifest.json must contain an inputs array."]

    errors: list[str] = []
    entries: dict[str, dict[str, Any]] = {}
    for entry in raw_inputs:
        if not isinstance(entry, dict):
            errors.append("Input manifest entries must be JSON objects.")
            continue
        filename = str(entry.get("filename") or entry.get("name") or "").strip()
        if not filename:
            errors.append("Input manifest entry is missing filename.")
            continue
        if filename in entries:
            errors.append(f"Input manifest contains duplicate filename {filename}.")
            continue
        entries[filename] = entry
        kind = str(entry.get("kind") or "").strip().lower()
        file_format = str(entry.get("format") or "").strip().lower().lstrip(".")
        if kind not in CANONICAL_ARTIFACT_KINDS:
            errors.append(
                f"Input {filename} has non-canonical kind '{kind or '<missing>'}'."
            )
        if not file_format:
            errors.append(f"Input {filename} is missing format.")

        raw_path = str(entry.get("path") or "").strip()
        relative_path = Path(raw_path)
        if not raw_path or relative_path.is_absolute() or ".." in relative_path.parts:
            errors.append(f"Input {filename} has unsafe or missing path '{raw_path}'.")
        else:
            input_path = bundle_root / relative_path
            if not input_path.is_file():
                errors.append(
                    f"Input {filename} references missing bundle file '{raw_path}'."
                )
            else:
                expected_size = entry.get("size_bytes")
                if expected_size is None:
                    errors.append(f"Input {filename} is missing size_bytes.")
                else:
                    try:
                        parsed_size = int(expected_size)
                    except (TypeError, ValueError):
                        errors.append(
                            f"Input {filename} has invalid size_bytes {expected_size!r}."
                        )
                    else:
                        actual_size = input_path.stat().st_size
                        if parsed_size != actual_size:
                            errors.append(
                                f"Input {filename} size mismatch: expected "
                                f"{parsed_size}, got {actual_size}."
                            )

                expected_digest = str(entry.get("sha256") or "").strip()
                if not expected_digest:
                    errors.append(f"Input {filename} is missing sha256.")
                else:
                    actual_digest = _sha256_file(input_path)
                    if expected_digest != actual_digest:
                        errors.append(
                            f"Input {filename} checksum mismatch: expected "
                            f"{expected_digest}, got {actual_digest}."
                        )

    descriptors = _root_contract_descriptors(bundle_root, bundle_manifest)
    for filename, descriptor in descriptors.items():
        entry = entries.get(filename)
        if entry is None:
            errors.append(
                f"Root node contract input {filename} is missing from inputs/input_manifest.json."
            )
            continue
        expected = _canonical_artifact(
            filename,
            kind=descriptor.get("kind"),
            file_format=descriptor.get("format"),
        )
        actual_kind = str(entry.get("kind") or "").strip().lower()
        actual_format = str(entry.get("format") or "").strip().lower().lstrip(".")
        if actual_kind != expected["kind"]:
            errors.append(
                f"Input {filename} kind '{actual_kind}' does not match root node "
                f"contract kind '{expected['kind']}'."
            )
        if actual_format != expected["format"]:
            errors.append(
                f"Input {filename} format '{actual_format}' does not match root node "
                f"contract format '{expected['format']}'."
            )
    return errors


def _validate_bundle_structure(
    bundle_root: Path, targets: dict[str, Any]
) -> dict[str, Any]:
    required_paths = [
        "bundle-manifest.json",
        "inputs",
        "nodes",
        "outputs",
    ]
    if targets.get("argo"):
        required_paths.extend(
            [
                "argo/workflow.yaml",
                "argo/Dockerfile",
                "argo/requirements.txt",
            ]
        )
    if targets.get("dagster"):
        required_paths.extend(
            [
                "dagster/pyproject.toml",
                "dagster/Dockerfile",
                "dagster/docker-compose.yml",
                "dagster/src/inlumen_dagster_project/definitions.py",
            ]
        )

    missing = []
    for item in required_paths:
        candidate = bundle_root / item
        if not candidate.exists():
            missing.append(item)

    manifest = _load_json(bundle_root / "bundle-manifest.json")
    manifest_version = str(manifest.get("schema_version") or "").strip()
    run_spec_path = str(manifest.get("run_spec") or "").strip()
    if run_spec_path and not (bundle_root / run_spec_path).is_file():
        missing.append(run_spec_path)
    node_entries = (
        manifest.get("nodes") if isinstance(manifest.get("nodes"), list) else []
    )
    missing_node_outputs = []
    for node in node_entries:
        if not isinstance(node, dict):
            continue
        node_path = str(node.get("path") or "").strip()
        output_path = str(node.get("output_path") or "").strip()
        if node_path and not (bundle_root / node_path).is_dir():
            missing.append(node_path)
        if output_path and not (bundle_root / output_path).is_dir():
            missing_node_outputs.append(output_path)

    errors = [f"Missing required bundle path: {item}" for item in missing]
    if manifest_version not in SUPPORTED_BUNDLE_MANIFEST_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_BUNDLE_MANIFEST_VERSIONS))
        errors.append(
            "bundle-manifest.json uses unsupported schema_version "
            f"{manifest_version or '<missing>'}; supported versions: {supported}."
        )
    errors.extend(
        f"Missing node output directory: {item}" for item in missing_node_outputs
    )
    if not missing:
        errors.extend(_input_contract_errors(bundle_root, manifest))
    run_spec = _load_json(bundle_root / run_spec_path) if run_spec_path else {}
    run_spec_version = str(run_spec.get("schema_version") or "").strip()
    if run_spec_path and run_spec_version not in SUPPORTED_RUN_SPEC_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_RUN_SPEC_VERSIONS))
        errors.append(
            f"{run_spec_path} uses unsupported schema_version "
            f"{run_spec_version or '<missing>'}; supported versions: {supported}."
        )
    elif run_spec_path:
        manifest_artifact_contract = (
            manifest.get("artifact_contract")
            if isinstance(manifest.get("artifact_contract"), dict)
            else {}
        )
        run_artifact_contract = (
            run_spec.get("artifact_contract")
            if isinstance(run_spec.get("artifact_contract"), dict)
            else {}
        )
        if manifest_version == "inlumen.deployment-bundle@2":
            if run_spec_version != "inlumen.run-spec@3":
                errors.append(
                    "inlumen.deployment-bundle@2 requires inlumen.run-spec@3."
                )
            for location, contract in (
                ("bundle-manifest.json", manifest_artifact_contract),
                ("run-spec.json", run_artifact_contract),
            ):
                if contract.get("schema_version") != "inlumen.artifact-contract@3":
                    errors.append(
                        f"{location} must use inlumen.artifact-contract@3 for "
                        "inlumen.deployment-bundle@2."
                    )
                if contract.get("port_namespaced") is not False:
                    errors.append(
                        f"{location} artifact contract must set port_namespaced=false."
                    )
        run_nodes = (
            run_spec.get("nodes") if isinstance(run_spec.get("nodes"), list) else []
        )
        run_node_ids = {
            str(node.get("id") or "")
            for node in run_nodes
            if isinstance(node, dict) and str(node.get("id") or "")
        }
        manifest_node_ids = {
            str(node.get("flow_id") or "")
            for node in node_entries
            if isinstance(node, dict) and str(node.get("flow_id") or "")
        }
        if run_node_ids != manifest_node_ids:
            errors.append(
                "run-spec.json node ids do not match bundle-manifest.json nodes."
            )
        runtime = (
            run_spec.get("runtime") if isinstance(run_spec.get("runtime"), dict) else {}
        )
        if runtime.get("package_manager") != "uv":
            errors.append("run-spec.json runtime.package_manager must be uv.")
        for connection in run_spec.get("connections") or []:
            if not isinstance(connection, dict):
                errors.append("run-spec.json connections must be objects.")
                continue
            source = str(connection.get("source") or "")
            target = str(connection.get("target") or "")
            if source not in run_node_ids or target not in run_node_ids:
                errors.append(
                    f"run-spec.json connection {source}->{target} references an unknown node."
                )
    return {
        "ok": not errors,
        "bundle_root": str(bundle_root),
        "manifest_schema": manifest.get("schema_version"),
        "node_count": len(node_entries),
        "errors": errors,
    }


def _write_json_if_changed(
    path: Path, payload: dict[str, Any], actions: list[str]
) -> None:
    next_text = json.dumps(payload, indent=2) + "\n"
    current_text = path.read_text(encoding="utf-8") if path.exists() else ""
    if current_text != next_text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(next_text, encoding="utf-8")
        actions.append(
            f"wrote {path.relative_to(_bundle_root(path)) if path.is_absolute() else path}"
        )


def _write_text_if_missing(path: Path, content: str, actions: list[str]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    actions.append(f"created {path.name}")


def _safe_node_entries(
    manifest: dict[str, Any], bundle_root: Path
) -> list[dict[str, Any]]:
    raw_nodes = manifest.get("nodes") if isinstance(manifest.get("nodes"), list) else []
    nodes = [node for node in raw_nodes if isinstance(node, dict)]
    if nodes:
        return nodes

    node_root = bundle_root / "nodes"
    if not node_root.is_dir():
        return []
    inferred = []
    for index, node_dir in enumerate(
        sorted(item for item in node_root.iterdir() if item.is_dir()), start=1
    ):
        flow_id = str(index)
        name_parts = node_dir.name.split("-", 2)
        if len(name_parts) > 1 and name_parts[1]:
            flow_id = name_parts[1]
        inferred.append(
            {
                "flow_id": flow_id,
                "label": node_dir.name,
                "type": "custom",
                "path": f"nodes/{node_dir.name}",
                "output_path": f"outputs/{node_dir.name}",
                "parents": [],
            }
        )
    return inferred


def _normalize_input_manifest(bundle_root: Path, actions: list[str]) -> None:
    input_dir = bundle_root / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = input_dir / "input_manifest.json"
    # New bundles hand off the directory itself.  Preserve and normalize a
    # manifest only when repairing a legacy export; never create one.
    if not manifest_path.exists():
        return
    manifest = _load_json(manifest_path)
    raw_entries = manifest.get("inputs")
    if not isinstance(raw_entries, list):
        raw_entries = (
            manifest.get("files") if isinstance(manifest.get("files"), list) else []
        )
    bundle_manifest = _load_json(bundle_root / "bundle-manifest.json")
    descriptors = _root_contract_descriptors(bundle_root, bundle_manifest)
    inputs = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        normalized = dict(entry)
        filename = str(
            normalized.get("filename") or normalized.get("name") or ""
        ).strip()
        if not filename:
            continue
        normalized["filename"] = filename
        normalized["path"] = f"inputs/{filename}"
        input_path = input_dir / filename
        if input_path.is_file():
            normalized["size_bytes"] = input_path.stat().st_size
            normalized["sha256"] = _sha256_file(input_path)
        descriptor = descriptors.get(filename, {})
        classification = _canonical_artifact(
            filename,
            kind=descriptor.get("kind") or normalized.get("kind"),
            file_format=descriptor.get("format") or normalized.get("format"),
        )
        normalized.update(classification)
        inputs.append(normalized)

    known_filenames = {entry["filename"] for entry in inputs}
    for input_file in sorted(
        item
        for item in input_dir.iterdir()
        if item.is_file() and item.name != "input_manifest.json"
    ):
        if input_file.name in known_filenames:
            continue
        descriptor = descriptors.get(input_file.name, {})
        inputs.append(
            {
                "filename": input_file.name,
                "path": f"inputs/{input_file.name}",
                "size_bytes": input_file.stat().st_size,
                "sha256": _sha256_file(input_file),
                **_canonical_artifact(
                    input_file.name,
                    kind=descriptor.get("kind"),
                    file_format=descriptor.get("format"),
                ),
                "description": "Input file supplied for this execution.",
            }
        )

    repaired = {
        "schema_version": "inlumen.input-manifest@1",
        "inputs": inputs,
    }
    before = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
    after = json.dumps(repaired, indent=2) + "\n"
    if before != after:
        manifest_path.write_text(after, encoding="utf-8")
        actions.append("normalized inputs/input_manifest.json")


def _compose_root_content() -> str:
    return """services:
  dagster:
    build:
      context: .
      dockerfile: dagster/Dockerfile
    ports:
      - "3000:3000"
    volumes:
      - ./outputs:/workspace/outputs
    environment:
      DAGSTER_HOME: /workspace/dagster/.dagster_home
      PYTHONUNBUFFERED: "1"
"""


def _compose_dagster_content() -> str:
    return """services:
  dagster:
    build:
      context: ..
      dockerfile: dagster/Dockerfile
    ports:
      - "3000:3000"
    volumes:
      - ../outputs:/workspace/outputs
    environment:
      DAGSTER_HOME: /workspace/dagster/.dagster_home
      PYTHONUNBUFFERED: "1"
"""


def _repair_dagster_defs(
    bundle_root: Path, nodes: list[dict[str, Any]], actions: list[str]
) -> None:
    defs_root = bundle_root / "dagster" / "src" / "inlumen_dagster_project" / "defs"
    if yaml is None or not defs_root.is_dir() or not nodes:
        return

    node_by_flow = {str(node.get("flow_id") or ""): node for node in nodes}
    node_order = [str(node.get("flow_id") or "") for node in nodes]
    defs_files = sorted(defs_root.glob("*/defs.yaml"))
    for index, defs_file in enumerate(defs_files):
        try:
            parsed = yaml.safe_load(defs_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            parsed = None
        if not isinstance(parsed, dict):
            continue
        attrs = (
            parsed.get("attributes")
            if isinstance(parsed.get("attributes"), dict)
            else {}
        )
        if not attrs:
            continue
        flow_id = node_order[index] if index < len(node_order) else ""
        node = node_by_flow.get(flow_id)
        if node is None:
            continue
        node_path = str(node.get("path") or "").strip()
        output_path = str(node.get("output_path") or "").strip()
        parents = [str(parent) for parent in node.get("parents") or []]
        input_dirs = [
            f"../{node_by_flow[parent].get('output_path')}"
            for parent in parents
            if parent in node_by_flow
        ] or ["../inputs"]
        attrs["script_path"] = f"../{node_path}/main.py"
        attrs["input_dirs"] = input_dirs
        attrs["input_dir"] = f"../workspaces/{Path(output_path).name}/input"
        attrs["output_dir"] = f"../{output_path}"
        for legacy_key in (
            "input_manifest_path",
            "output_manifest_path",
            "context_path",
        ):
            attrs.pop(legacy_key, None)
        parsed["attributes"] = attrs
        next_text = yaml.safe_dump(parsed, sort_keys=False)
        if defs_file.read_text(encoding="utf-8") != next_text:
            defs_file.write_text(next_text, encoding="utf-8")
            actions.append(f"normalized {defs_file.relative_to(bundle_root)}")


def repair_deployment_bundle(
    path: Path,
    *,
    targets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle_root = _bundle_root(path)
    actions: list[str] = []
    bundle_root.mkdir(parents=True, exist_ok=True)

    manifest_path = bundle_root / "bundle-manifest.json"
    manifest = _load_json(manifest_path)
    manifest_targets = (
        manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
    )
    selected_targets = {
        "argo": bool(
            (targets or {}).get(
                "argo", manifest_targets.get("argo", (bundle_root / "argo").exists())
            )
        ),
        "dagster": bool(
            (targets or {}).get(
                "dagster",
                manifest_targets.get(
                    "dagster",
                    (bundle_root / "dagster").exists()
                    or (bundle_root / "dagster_project").exists(),
                ),
            )
        ),
    }

    for dirname in ("inputs", "nodes", "outputs"):
        directory = bundle_root / dirname
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            actions.append(f"created {dirname}/")

    _normalize_input_manifest(bundle_root, actions)
    nodes = _safe_node_entries(manifest, bundle_root)
    repaired_nodes = []
    for node in nodes:
        repaired = dict(node)
        flow_id = str(repaired.get("flow_id") or "").strip()
        node_path = str(repaired.get("path") or "").strip()
        if not node_path:
            node_path = f"nodes/node-{flow_id or len(repaired_nodes) + 1}"
            repaired["path"] = node_path
        output_path = str(repaired.get("output_path") or "").strip()
        if not output_path:
            output_path = f"outputs/{Path(node_path).name}"
            repaired["output_path"] = output_path
        (bundle_root / node_path).mkdir(parents=True, exist_ok=True)
        output_dir = bundle_root / output_path
        output_dir.mkdir(parents=True, exist_ok=True)
        gitkeep = output_dir / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")
            actions.append(f"created {Path(output_path) / '.gitkeep'}")
        repaired["parents"] = [str(parent) for parent in repaired.get("parents") or []]
        repaired_nodes.append(repaired)

    if not str(manifest.get("schema_version") or "").strip():
        manifest["schema_version"] = "inlumen.deployment-bundle@1"
    manifest["targets"] = selected_targets
    manifest["node_order"] = [
        str(node.get("flow_id") or "")
        for node in repaired_nodes
        if str(node.get("flow_id") or "")
    ]
    manifest["nodes"] = repaired_nodes
    manifest["inputs"] = {
        "path": "inputs",
        "file_count": sum(
            1 for path in (bundle_root / "inputs").rglob("*") if path.is_file()
        ),
        "lifecycle": "per-run",
        "transport": "filesystem",
    }
    manifest["outputs"] = {
        "path": "outputs",
        "per_node": [str(node.get("output_path") or "") for node in repaired_nodes],
    }
    if selected_targets["argo"]:
        manifest["argo"] = {"workflow": "argo/workflow.yaml"}
    if selected_targets["dagster"]:
        manifest["dagster"] = {
            "project": "dagster",
            "dockerfile": "dagster/Dockerfile",
            "compose": "docker-compose.yml",
            "project_compose": "dagster/docker-compose.yml",
        }
        _write_text_if_missing(
            bundle_root / "docker-compose.yml", _compose_root_content(), actions
        )
        _write_text_if_missing(
            bundle_root / "dagster" / "docker-compose.yml",
            _compose_dagster_content(),
            actions,
        )
        _repair_dagster_defs(bundle_root, repaired_nodes, actions)

    next_manifest = json.dumps(manifest, indent=2) + "\n"
    if (
        not manifest_path.exists()
        or manifest_path.read_text(encoding="utf-8") != next_manifest
    ):
        manifest_path.write_text(next_manifest, encoding="utf-8")
        actions.append("normalized bundle-manifest.json")

    return {
        "ok": True,
        "bundle_root": str(bundle_root),
        "actions": actions,
        "changed": bool(actions),
    }


def validate_and_repair_deployment_bundle(
    path: Path,
    *,
    targets: dict[str, Any] | None = None,
    validate_argo: bool | None = None,
    validate_dagster: bool | None = None,
    materialize: bool = True,
    reinstall: bool = False,
    skip_install: bool = False,
    argo_lint: bool = False,
    argo_dry_run: bool = False,
    timeout_seconds: int = 900,
    runtime_secrets: dict[str, str] | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    repair_report = repair_deployment_bundle(path, targets=targets)
    validation_report = validate_deployment_bundle(
        path,
        targets=targets,
        validate_argo=validate_argo,
        validate_dagster=validate_dagster,
        materialize=materialize,
        reinstall=reinstall,
        skip_install=skip_install,
        argo_lint=argo_lint,
        argo_dry_run=argo_dry_run,
        timeout_seconds=timeout_seconds,
        runtime_secrets=runtime_secrets,
        execution_id=execution_id,
    )
    return {
        "ok": bool(repair_report.get("ok") and validation_report.get("ok")),
        "bundle_root": validation_report.get("bundle_root")
        or repair_report.get("bundle_root"),
        "repair_report": repair_report,
        "validation_report": validation_report,
        "errors": validation_report.get("errors") or [],
    }


def _find_argo_workflow(bundle_root: Path) -> Path | None:
    preferred = bundle_root / "argo" / "workflow.yaml"
    if preferred.is_file():
        return preferred
    argo_dir = bundle_root / "argo"
    if argo_dir.is_dir():
        for candidate in sorted([*argo_dir.glob("*.yaml"), *argo_dir.glob("*.yml")]):
            if candidate.is_file():
                return candidate
    if bundle_root.suffix.lower() in {".yaml", ".yml"} and bundle_root.is_file():
        return bundle_root
    return None


def _validate_argo_static(workflow_path: Path) -> dict[str, Any]:
    errors: list[str] = []
    if yaml is None:
        return {
            "ok": False,
            "workflow_path": str(workflow_path),
            "errors": ["PyYAML is unavailable; cannot parse Argo workflow YAML."],
        }
    try:
        docs = [
            doc
            for doc in yaml.safe_load_all(workflow_path.read_text(encoding="utf-8"))
            if doc is not None
        ]
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return {
            "ok": False,
            "workflow_path": str(workflow_path),
            "errors": [f"YAML parsing failed: {exc}"],
        }
    if len(docs) != 1 or not isinstance(docs[0], dict):
        return {
            "ok": False,
            "workflow_path": str(workflow_path),
            "errors": ["Argo workflow YAML must contain exactly one mapping document."],
        }

    workflow = docs[0]
    if workflow.get("apiVersion") != "argoproj.io/v1alpha1":
        errors.append("apiVersion must be argoproj.io/v1alpha1")
    if workflow.get("kind") not in {
        "Workflow",
        "WorkflowTemplate",
        "ClusterWorkflowTemplate",
        "CronWorkflow",
    }:
        errors.append(
            "kind must be Workflow, WorkflowTemplate, ClusterWorkflowTemplate, or CronWorkflow"
        )
    spec = workflow.get("spec") if isinstance(workflow.get("spec"), dict) else {}
    entrypoint = spec.get("entrypoint")
    templates = spec.get("templates") if isinstance(spec.get("templates"), list) else []
    if not entrypoint:
        errors.append("spec.entrypoint is required")
    if not templates:
        errors.append("spec.templates must be a non-empty list")

    template_by_name = {
        template.get("name"): template
        for template in templates
        if isinstance(template, dict) and template.get("name")
    }
    if entrypoint and entrypoint not in template_by_name:
        errors.append(f"entrypoint template '{entrypoint}' is missing")

    for template in templates:
        if not isinstance(template, dict):
            continue
        name = str(template.get("name") or "")
        if not name:
            errors.append("template missing name")
            continue
        if "dag" in template:
            tasks = (template.get("dag") or {}).get("tasks") or []
            if not isinstance(tasks, list):
                errors.append(f"template '{name}' dag.tasks must be a list")
                continue
            task_names = {
                str(task.get("name") or "") for task in tasks if isinstance(task, dict)
            }
            for task in tasks:
                if not isinstance(task, dict):
                    errors.append(f"template '{name}' has non-object DAG task")
                    continue
                task_name = str(task.get("name") or "")
                task_template = str(task.get("template") or "")
                if not task_name or not task_template:
                    errors.append(
                        f"template '{name}' DAG task requires name and template"
                    )
                if task_template and task_template not in template_by_name:
                    errors.append(
                        f"task '{task_name}' references missing template '{task_template}'"
                    )
                for dependency in task.get("dependencies") or []:
                    if str(dependency) not in task_names:
                        errors.append(
                            f"task '{task_name}' references missing dependency '{dependency}'"
                        )
        elif not any(
            key in template
            for key in ("container", "script", "steps", "resource", "suspend", "http")
        ):
            errors.append(
                f"template '{name}' does not define an executable or control body"
            )

    return {
        "ok": not errors,
        "workflow_path": str(workflow_path),
        "kind": workflow.get("kind"),
        "template_count": len(templates),
        "errors": errors,
    }


def _find_working_argo() -> str | None:
    candidates: list[str] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / "argo"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            candidate_text = str(candidate)
            if candidate_text not in candidates:
                candidates.append(candidate_text)

    for candidate in candidates:
        try:
            proc = subprocess.run(
                [candidate, "version", "--short"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return candidate
    return None


def _run_argo_command(
    command: list[str], *, cwd: Path, timeout_seconds: int
) -> dict[str, Any]:
    executable = _find_working_argo()
    if not executable:
        return {
            "command": command,
            "ok": True,
            "skipped": True,
            "output": "argo CLI is not installed; optional CLI validation skipped.",
            "returncode": None,
        }
    return _run([executable, *command[1:]], cwd=cwd, timeout_seconds=timeout_seconds)


def validate_argo_workflow(
    path: Path,
    *,
    argo_lint: bool = False,
    argo_dry_run: bool = False,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    bundle_root = _bundle_root(path)
    workflow_path = _find_argo_workflow(bundle_root)
    if workflow_path is None:
        return {
            "ok": False,
            "bundle_root": str(bundle_root),
            "errors": ["No Argo workflow YAML found under argo/workflow.yaml."],
            "steps": [],
        }

    steps: list[dict[str, Any]] = []
    static_report = _validate_argo_static(workflow_path)
    steps.append({"name": "static", **static_report})
    if not static_report["ok"]:
        return {
            "ok": False,
            "bundle_root": str(bundle_root),
            "workflow_path": str(workflow_path),
            "steps": steps,
            "errors": static_report["errors"],
        }

    if argo_lint:
        lint_step = _run_argo_command(
            ["argo", "lint", str(workflow_path)],
            cwd=bundle_root,
            timeout_seconds=timeout_seconds,
        )
        lint_step["name"] = "argo_lint"
        steps.append(lint_step)
        if not lint_step.get("ok"):
            return {
                "ok": False,
                "bundle_root": str(bundle_root),
                "workflow_path": str(workflow_path),
                "steps": steps,
                "errors": ["argo lint failed."],
            }

    if argo_dry_run:
        dry_run_step = _run_argo_command(
            ["argo", "submit", "--dry-run", "-o", "yaml", str(workflow_path)],
            cwd=bundle_root,
            timeout_seconds=timeout_seconds,
        )
        dry_run_step["name"] = "argo_submit_dry_run"
        steps.append(dry_run_step)
        if not dry_run_step.get("ok"):
            return {
                "ok": False,
                "bundle_root": str(bundle_root),
                "workflow_path": str(workflow_path),
                "steps": steps,
                "errors": ["argo submit --dry-run failed."],
            }

    return {
        "ok": True,
        "bundle_root": str(bundle_root),
        "workflow_path": str(workflow_path),
        "steps": steps,
    }


def validate_deployment_bundle(
    path: Path,
    *,
    targets: dict[str, Any] | None = None,
    validate_argo: bool | None = None,
    validate_dagster: bool | None = None,
    materialize: bool = True,
    reinstall: bool = False,
    skip_install: bool = False,
    argo_lint: bool = False,
    argo_dry_run: bool = False,
    timeout_seconds: int = 900,
    runtime_secrets: dict[str, str] | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    bundle_root = _bundle_root(path)
    manifest = _load_json(bundle_root / "bundle-manifest.json")
    manifest_targets = (
        manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
    )
    selected_targets = {
        "argo": bool(
            (targets or {}).get(
                "argo", manifest_targets.get("argo", (bundle_root / "argo").exists())
            )
        ),
        "dagster": bool(
            (targets or {}).get(
                "dagster",
                manifest_targets.get(
                    "dagster",
                    (bundle_root / "dagster").exists()
                    or (bundle_root / "dagster_project").exists(),
                ),
            )
        ),
    }
    if validate_argo is None:
        validate_argo = selected_targets["argo"]
    if validate_dagster is None:
        validate_dagster = selected_targets["dagster"]

    report: dict[str, Any] = {
        "ok": False,
        "bundle_root": str(bundle_root),
        "targets": selected_targets,
        "structure": {},
        "argo": None,
        "dagster": None,
        "errors": [],
    }

    structure_report = _validate_bundle_structure(bundle_root, selected_targets)
    report["structure"] = structure_report
    if not structure_report["ok"]:
        report["errors"].extend(structure_report["errors"])

    if validate_argo:
        argo_report = validate_argo_workflow(
            bundle_root,
            argo_lint=argo_lint,
            argo_dry_run=argo_dry_run,
            timeout_seconds=min(timeout_seconds, 300),
        )
        report["argo"] = argo_report
        if not argo_report.get("ok"):
            report["errors"].extend(
                argo_report.get("errors") or ["Argo validation failed."]
            )

    if validate_dagster:
        dagster_report = validate_dagster_project(
            bundle_root,
            materialize=materialize,
            reinstall=reinstall,
            skip_install=skip_install,
            timeout_seconds=timeout_seconds,
            runtime_secrets=runtime_secrets,
            execution_id=execution_id,
        )
        report["dagster"] = dagster_report
        if not dagster_report.get("ok"):
            report["errors"].extend(
                dagster_report.get("errors") or ["Dagster validation failed."]
            )

    report["ok"] = not report["errors"]
    return report


_BINARY_EXTENSIONS = {
    ".7z",
    ".aac",
    ".bin",
    ".bmp",
    ".flac",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".joblib",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".npy",
    ".npz",
    ".ogg",
    ".onnx",
    ".parquet",
    ".pdf",
    ".pickle",
    ".pkl",
    ".png",
    ".pt",
    ".pth",
    ".tar",
    ".tif",
    ".tiff",
    ".wav",
    ".webp",
    ".zip",
}


def _safe_bundle_relative_path(path: Any) -> Path:
    raw_path = str(path or "").strip()
    relative = Path(raw_path)
    if (
        not raw_path
        or raw_path in {".", ".."}
        or relative.is_absolute()
        or any(part == ".." for part in relative.parts)
    ):
        raise ValueError(f"Unsafe bundle file path: {raw_path or '<empty>'}")
    return relative


def _decode_artifact_content(file_entry: dict[str, Any]) -> bytes:
    content = file_entry.get("content")
    text = content if isinstance(content, str) else str(content or "")
    encoding = str(file_entry.get("content_encoding") or "utf-8").strip().lower()
    if encoding == "base64":
        try:
            return base64.b64decode(text, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError(
                f"Invalid base64 content for {file_entry.get('path') or file_entry.get('filename')}"
            ) from exc
    if encoding in {"", "utf-8", "text", "plain"}:
        return text.encode("utf-8")
    raise ValueError(f"Unsupported artifact content encoding: {encoding}")


def _verify_artifact_integrity(
    file_entry: dict[str, Any],
    content: bytes,
) -> None:
    label = file_entry.get("path") or file_entry.get("filename")
    expected_size = file_entry.get("size_bytes")
    if expected_size is not None and int(expected_size) != len(content):
        raise ValueError(
            f"Artifact size mismatch for {label}: expected {expected_size}, got {len(content)}"
        )
    expected_digest = str(file_entry.get("sha256") or "").strip()
    if expected_digest:
        actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if expected_digest != actual_digest:
            raise ValueError(f"Artifact checksum mismatch for {label}")


def _materialize_bundle_files(
    files: list[dict[str, Any]],
    bundle_root: Path,
) -> None:
    seen_paths: set[Path] = set()
    for file_entry in files:
        if not isinstance(file_entry, dict):
            raise TypeError("Deployment bundle files must be JSON objects.")
        relative_path = _safe_bundle_relative_path(file_entry.get("path"))
        if relative_path in seen_paths:
            raise ValueError(f"Duplicate bundle file path: {relative_path.as_posix()}")
        seen_paths.add(relative_path)
        content = _decode_artifact_content(file_entry)
        _verify_artifact_integrity(file_entry, content)
        destination = bundle_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _content_type_for_filename(filename: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _encode_artifact_bytes(
    content: bytes,
    *,
    filename: str,
    content_type: str = "",
) -> dict[str, Any]:
    resolved_content_type = _content_type_for_filename(filename, content_type)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if Path(filename).suffix.lower() not in _BINARY_EXTENSIONS:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            return {
                "content": text,
                "content_encoding": "utf-8",
                "content_type": resolved_content_type,
                "size_bytes": len(content),
                "sha256": digest,
            }
    return {
        "content": base64.b64encode(content).decode("ascii"),
        "content_encoding": "base64",
        "content_type": resolved_content_type,
        "size_bytes": len(content),
        "sha256": digest,
        "encoding": "base64",
    }


def _skip_validation_bundle_file(path: Path, bundle_root: Path) -> bool:
    try:
        relative = path.relative_to(bundle_root)
    except ValueError:
        return True
    parts = set(relative.parts)
    if parts & {".inlumen_dagster_validation_venv", ".dagster_home", "__pycache__"}:
        return True
    if path.suffix in {".pyc", ".pyo"}:
        return True
    return bool(
        relative.parts and relative.parts[0] == "outputs" and path.name != ".gitkeep"
    )


def _artifact_file_entry(
    path: Path,
    bundle_root: Path,
    *,
    role: str,
) -> dict[str, Any]:
    relative = path.relative_to(bundle_root).as_posix()
    encoded = _encode_artifact_bytes(
        path.read_bytes(),
        filename=path.name,
        content_type=(
            "application/json"
            if path.suffix == ".json"
            else "application/x-yaml;charset=utf-8"
            if path.suffix in {".yaml", ".yml"}
            else ""
        ),
    )
    return {
        "path": relative,
        "filename": path.name,
        "flow_id": "",
        **_canonical_artifact(path.name),
        **encoded,
        "role": role,
    }


def _read_repaired_bundle_files(bundle_root: Path) -> list[dict[str, Any]]:
    return [
        _artifact_file_entry(path, bundle_root, role="runtime")
        for path in sorted(item for item in bundle_root.rglob("*") if item.is_file())
        if not _skip_validation_bundle_file(path, bundle_root)
    ]


def _read_run_output_files(bundle_root: Path) -> list[dict[str, Any]]:
    output_root = bundle_root / "outputs"
    if not output_root.is_dir():
        return []
    entries = []
    for path in sorted(item for item in output_root.rglob("*") if item.is_file()):
        if path.name == ".gitkeep":
            continue
        entry = _artifact_file_entry(path, bundle_root, role="run-output")
        relative = path.relative_to(output_root)
        # Dagster uses outputs/<node>/<run-id>/output/<filename> internally.
        # A run already scopes its own artifact collection, so engine-private
        # staging directories must not leak into the public artifact contract.
        if len(relative.parts) >= 4 and relative.parts[2] == "output":
            entry["path"] = Path(
                "outputs", relative.parts[0], *relative.parts[3:]
            ).as_posix()
        entries.append(entry)
    return entries


def validate_deployment_bundle_files(
    files: list[dict[str, Any]],
    *,
    targets: dict[str, Any] | None = None,
    mode: str = "validate",
    validate_argo: bool | None = None,
    validate_dagster: bool | None = None,
    materialize: bool = True,
    reinstall: bool = False,
    skip_install: bool = False,
    argo_lint: bool = False,
    argo_dry_run: bool = False,
    timeout_seconds: int = 900,
    runtime_secrets: dict[str, str] | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """Materialize, validate, and optionally repair an uploaded bundle.

    The codegen API accepts bundle contents instead of a host filesystem path,
    so the backend and codegen service can be deployed independently without a
    shared volume or container-specific path translation.
    """
    normalized_mode = str(mode or "validate").strip().lower()
    if normalized_mode not in {"fast", "validate", "repair", "validate-and-repair"}:
        raise ValueError(f"Unsupported deployment validation mode: {normalized_mode}")

    workspace_parent = (
        os.getenv("CODEGEN_DEPLOYMENT_VALIDATION_WORKDIR", "").strip()
        or os.getenv("CODEGEN_VALIDATION_WORKDIR", "").strip()
    )
    if workspace_parent:
        Path(workspace_parent).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="inlumen-deployment-validation-",
        dir=workspace_parent or None,
    ) as temp_dir:
        bundle_root = Path(temp_dir)
        _materialize_bundle_files(files, bundle_root)

        repair_report = None
        if normalized_mode in {"repair", "validate-and-repair"}:
            service_report = validate_and_repair_deployment_bundle(
                bundle_root,
                targets=targets,
                validate_argo=validate_argo,
                validate_dagster=validate_dagster,
                materialize=materialize,
                reinstall=reinstall,
                skip_install=skip_install,
                argo_lint=argo_lint,
                argo_dry_run=argo_dry_run,
                timeout_seconds=timeout_seconds,
                runtime_secrets=runtime_secrets,
                execution_id=execution_id,
            )
            repair_report = service_report.get("repair_report")
            validation_report = service_report.get("validation_report")
            if not isinstance(validation_report, dict):
                validation_report = service_report
        else:
            validation_report = validate_deployment_bundle(
                bundle_root,
                targets=targets,
                validate_argo=validate_argo,
                validate_dagster=validate_dagster,
                materialize=materialize,
                reinstall=reinstall,
                skip_install=skip_install,
                argo_lint=argo_lint,
                argo_dry_run=argo_dry_run,
                timeout_seconds=timeout_seconds,
                runtime_secrets=runtime_secrets,
                execution_id=execution_id,
            )

        ok = bool(validation_report.get("ok"))
        execution_requested = (
            normalized_mode != "fast"
            and bool((targets or {}).get("dagster"))
            and materialize
        )
        return {
            "ok": ok,
            "validation_report": validation_report,
            "repair_report": repair_report,
            "repaired_files": (
                _read_repaired_bundle_files(bundle_root)
                if ok and repair_report is not None
                else []
            ),
            "run_outputs": (
                _read_run_output_files(bundle_root)
                if ok and execution_requested
                else []
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an InLumen generated deployment bundle."
    )
    parser.add_argument("bundle_path")
    parser.add_argument(
        "--target", choices=["argo", "dagster"], action="append", default=[]
    )
    parser.add_argument(
        "--dagster-only",
        action="store_true",
        help="Run the legacy Dagster project validator only.",
    )
    parser.add_argument("--no-materialize", action="store_true")
    parser.add_argument("--reinstall", action="store_true")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--argo-lint", action="store_true")
    parser.add_argument("--argo-dry-run", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    if args.dagster_only:
        report = validate_dagster_project(
            Path(args.bundle_path),
            materialize=not args.no_materialize,
            reinstall=args.reinstall,
            skip_install=args.skip_install,
            timeout_seconds=args.timeout_seconds,
        )
    else:
        targets = (
            {
                "argo": "argo" in args.target,
                "dagster": "dagster" in args.target,
            }
            if args.target
            else None
        )
        report = validate_deployment_bundle(
            Path(args.bundle_path),
            targets=targets,
            materialize=not args.no_materialize,
            reinstall=args.reinstall,
            skip_install=args.skip_install,
            argo_lint=args.argo_lint,
            argo_dry_run=args.argo_dry_run,
            timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
