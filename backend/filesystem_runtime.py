"""Engine-neutral filesystem hand-off used by the pipeline runtime.

This module deliberately knows nothing about Dagster, node schemas, or user
code.  A runner prepares a fresh ``input`` directory for each node, executes
the node with the two public environment variables, then inventories the
resulting output directory for the orchestration layer.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Iterable


PIPELINE_INPUT_DIR = "PIPELINE_INPUT_DIR"
PIPELINE_OUTPUT_DIR = "PIPELINE_OUTPUT_DIR"
_DIRECT_PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_PARAMETER_ENVIRONMENT = {
    PIPELINE_INPUT_DIR,
    PIPELINE_OUTPUT_DIR,
    "PIPELINE_PARAMS_JSON",
}


def direct_parameter_environment_name(key: object) -> str:
    """Return the direct Task environment name for a safe parameter key."""
    name = str(key).strip()
    if (
        not _DIRECT_PARAMETER_NAME.fullmatch(name)
        or name in _RESERVED_PARAMETER_ENVIRONMENT
        or name.startswith(("PIPELINE_", "INLUMEN_"))
    ):
        return ""
    return name


def parameter_environment(parameters: dict[object, object]) -> dict[str, str]:
    """Encode configured Task parameters for a process environment.

    A parameter named ``QUESTION`` is available as ``os.getenv("QUESTION")``.
    The prefixed aliases remain available for older generated Task code.
    """
    normalized = {
        str(key): value
        for key, value in parameters.items()
        if str(key).strip() and str(key) != "model_plan"
    }
    environment: dict[str, str] = {}
    if normalized:
        environment["PIPELINE_PARAMS_JSON"] = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True
        )
    for key, value in sorted(normalized.items()):
        legacy_name = "PIPELINE_PARAM_" + "".join(
            char if char.isalnum() else "_" for char in key.upper()
        ).strip("_")
        if legacy_name != "PIPELINE_PARAM_":
            environment[legacy_name] = str(value)
        direct_name = direct_parameter_environment_name(key)
        if direct_name:
            environment[direct_name] = str(value)
    return environment


def task_environment(input_dir: Path | str, output_dir: Path | str) -> dict[str, str]:
    """Return the complete public environment contract for a Task process."""
    return {
        PIPELINE_INPUT_DIR: str(Path(input_dir).resolve()),
        PIPELINE_OUTPUT_DIR: str(Path(output_dir).resolve()),
    }


def _copy_entry(source: Path, destination: Path) -> None:
    if source.is_symlink():
        # Inputs are materialized rather than linked so a Task cannot follow a
        # link out of the workspace after staging has completed.
        source = source.resolve(strict=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def stage_input_directories(
    source_directories: Iterable[Path | str],
    input_dir: Path | str,
) -> Path:
    """Create a Task input directory from zero or more upstream directories.

    Files retain their relative paths.  Two upstream producers may not write
    different artifacts to the same path: silently picking one would make the
    pipeline nondeterministic.
    """
    destination_root = Path(input_dir)
    if destination_root.exists():
        shutil.rmtree(destination_root)
    destination_root.mkdir(parents=True, exist_ok=True)

    owners: dict[Path, Path] = {}
    for raw_source in source_directories:
        source_root = Path(raw_source)
        if not source_root.is_dir():
            continue
        for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            if source.is_dir():
                continue
            relative = source.relative_to(source_root)
            # Compatibility metadata from older bundle versions is not an
            # artifact and must not leak into the next Task workspace.
            if relative.as_posix() in {"input_manifest.json", "output_manifest.json"}:
                continue
            destination = destination_root / relative
            existing = owners.get(relative)
            if existing is not None:
                if source.read_bytes() == existing.read_bytes():
                    continue
                raise RuntimeError(
                    "Upstream artifacts collide at "
                    f"{relative.as_posix()!r}; add a Task that merges or renames them."
                )
            _copy_entry(source, destination)
            owners[relative] = source
    return destination_root


def stage_input_bindings(
    bindings: Iterable[dict[str, object]],
    input_dir: Path | str,
) -> Path:
    """Stage connected output ports directly into the Task input directory.

    Each binding contains ``source_dir``, ``source_port``, and ``target_port``.
    Only the connected source port is exposed to the consumer, but port names
    are orchestration metadata and never become public workspace directories.
    Artifact-owned relative paths are preserved and collisions fail loudly.
    A binding with empty ports is reserved for a Source node's run input.
    """
    destination_root = Path(input_dir)
    if destination_root.exists():
        shutil.rmtree(destination_root)
    destination_root.mkdir(parents=True, exist_ok=True)

    owners: dict[Path, Path] = {}
    for binding in bindings:
        source_root = Path(str(binding.get("source_dir") or ""))
        source_port = str(binding.get("source_port") or "").strip()
        if source_port:
            source_root = source_root / source_port
        if not source_root.is_dir():
            if binding.get("required", True):
                raise RuntimeError(
                    f"Required upstream artifact port is missing: {source_root}."
                )
            continue
        for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            if relative.as_posix() in {"input_manifest.json", "output_manifest.json", ".gitkeep"}:
                continue
            destination = destination_root / relative
            destination_relative = destination.relative_to(destination_root)
            existing = owners.get(destination_relative)
            if existing is not None:
                if source.read_bytes() == existing.read_bytes():
                    continue
                raise RuntimeError(
                    "Upstream artifacts collide at "
                    f"{destination_relative.as_posix()!r}; add a Task that merges or renames them."
                )
            _copy_entry(source, destination)
            owners[destination_relative] = source
    return destination_root


def normalize_single_output_port(
    output_dir: Path | str,
    output_ports: Iterable[str],
) -> list[str]:
    """Move public root outputs into the orchestration-owned output port.

    Task code always writes directly to ``PIPELINE_OUTPUT_DIR``.  For a Task
    with one output port, the runner namespaces those artifacts only after the
    process exits so downstream routing remains internal to the orchestrator.
    Multiple-output Tasks still require an explicit routing mechanism.
    """
    root = Path(output_dir)
    ports = [str(port).strip() for port in output_ports if str(port).strip()]
    root.mkdir(parents=True, exist_ok=True)
    declared = set(ports)
    root_entries = [
        path
        for path in sorted(root.iterdir(), key=lambda item: item.as_posix())
        if path.name not in declared and path.name != ".gitkeep"
    ]
    if not root_entries:
        return []
    if len(ports) != 1:
        names = ", ".join(path.name for path in root_entries)
        raise RuntimeError(
            "Task wrote artifacts outside declared output-port directories: "
            f"{names}."
        )
    port_root = root / ports[0]
    port_root.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for source in root_entries:
        destination = port_root / source.name
        if destination.exists():
            raise RuntimeError(
                f"Legacy root output {source.name!r} collides with {ports[0]!r} output."
            )
        shutil.move(str(source), str(destination))
        moved.append(source.name)
    return moved


def prepare_workspace(
    workspace_dir: Path | str,
    source_directories: Iterable[Path | str],
) -> tuple[Path, Path]:
    """Create the standard ``/workspace/input`` and ``/workspace/output`` layout."""
    workspace = Path(workspace_dir)
    input_dir = stage_input_directories(source_directories, workspace / "input")
    output_dir = workspace / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return input_dir, output_dir


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_artifacts(output_dir: Path | str) -> list[dict[str, object]]:
    """Inventory files produced by a Task for runtime/provenance use only.

    This metadata is generated *after* execution.  It is intentionally not an
    input/output schema and is never required from user code.
    """
    root = Path(output_dir)
    if not root.is_dir():
        return []
    artifacts: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        artifacts.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "content_type": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            }
        )
    return artifacts


def filesystem_shell_component_source() -> str:
    """Return the self-contained Dagster component shipped in an export.

    Generated bundles must run independently of the control-plane package, so
    the small runtime implementation is emitted as source rather than imported
    from this module.
    """
    return r'''import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import dagster as dg


_DIRECT_PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_PARAMETER_ENVIRONMENT = {
    "PIPELINE_INPUT_DIR",
    "PIPELINE_OUTPUT_DIR",
    "PIPELINE_PARAMS_JSON",
}


def _direct_parameter_environment_name(key):
    name = str(key).strip()
    if (
        not _DIRECT_PARAMETER_NAME.fullmatch(name)
        or name in _RESERVED_PARAMETER_ENVIRONMENT
        or name.startswith(("PIPELINE_", "INLUMEN_"))
    ):
        return ""
    return name


def _stage_inputs(source_dirs, input_dir):
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    owners = {}
    for source_root in source_dirs:
        source_root = Path(source_root)
        if not source_root.is_dir():
            continue
        for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            if relative.as_posix() in {"input_manifest.json", "output_manifest.json"}:
                continue
            destination = input_dir / relative
            existing = owners.get(relative)
            if existing is not None:
                if source.read_bytes() == existing.read_bytes():
                    continue
                raise RuntimeError(
                    f"Upstream artifacts collide at {relative.as_posix()!r}; "
                    "add a Task that merges or renames them."
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            owners[relative] = source


def _stage_input_bindings(bindings, input_dir):
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    owners = {}
    for binding in bindings:
        source_root = Path(str(binding.get("source_dir") or ""))
        source_port = str(binding.get("source_port") or "").strip()
        if source_port:
            source_root = source_root / source_port
        if not source_root.is_dir():
            if binding.get("required", True):
                raise RuntimeError(
                    f"Required upstream artifact port is missing: {source_root}."
                )
            continue
        for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            if relative.as_posix() in {
                "input_manifest.json", "output_manifest.json", ".gitkeep"
            }:
                continue
            destination = input_dir / relative
            destination_relative = destination.relative_to(input_dir)
            existing = owners.get(destination_relative)
            if existing is not None:
                if source.read_bytes() == existing.read_bytes():
                    continue
                raise RuntimeError(
                    f"Upstream artifacts collide at {destination_relative.as_posix()!r}; "
                    "add a Task that merges or renames them."
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            owners[destination_relative] = source


def _normalize_single_output_port(output_dir, output_ports):
    ports = [str(port).strip() for port in output_ports if str(port).strip()]
    output_dir.mkdir(parents=True, exist_ok=True)
    declared = set(ports)
    root_entries = [
        path for path in sorted(output_dir.iterdir(), key=lambda item: item.as_posix())
        if path.name not in declared and path.name != ".gitkeep"
    ]
    if not root_entries:
        return []
    if len(ports) != 1:
        names = ", ".join(path.name for path in root_entries)
        raise RuntimeError(
            "Task wrote artifacts outside declared output-port directories: " + names
        )
    port_root = output_dir / ports[0]
    port_root.mkdir(parents=True, exist_ok=True)
    moved = []
    for source in root_entries:
        destination = port_root / source.name
        if destination.exists():
            raise RuntimeError(
                f"Legacy root output {source.name!r} collides with {ports[0]!r} output."
            )
        shutil.move(str(source), str(destination))
        moved.append(source.name)
    return moved


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifacts(output_dir):
    results = []
    for path in sorted(output_dir.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_file():
            results.append({
                "path": path.relative_to(output_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "content_type": mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
            })
    return results


class ShellCommand(dg.Component, dg.Model, dg.Resolvable):
    asset_key: str
    script_path: str
    upstream_assets: list[str] = []
    input_dirs: list[str] = []
    input_bindings: list[dict] = []
    input_dir: str
    output_dir: str
    output_ports: list[str] = []
    arguments: list[str] = []
    parameters: dict = {}
    secret_environment: dict = {}
    runtime_environment: list[dict] = []

    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        deps = [dg.AssetKey(asset_key) for asset_key in self.upstream_assets]

        @dg.asset(name=self.asset_key, deps=deps)
        def run_script(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
            project_root = Path.cwd()

            def resolve(value):
                path = Path(value)
                return path if path.is_absolute() else project_root / path

            run_scope = re.sub(
                r"[^A-Za-z0-9_.-]+", "-", str(context.run_id)
            ).strip("-._") or "run"
            input_dir = resolve(self.input_dir) / run_scope
            output_dir = resolve(self.output_dir) / run_scope
            bindings = []
            for raw_binding in self.input_bindings:
                binding = dict(raw_binding)
                source_dir = resolve(binding.get("source_dir") or ".")
                if binding.get("run_scoped", True):
                    source_dir = source_dir / run_scope
                binding["source_dir"] = str(source_dir)
                bindings.append(binding)
            if bindings:
                _stage_input_bindings(bindings, input_dir)
            else:
                _stage_inputs([resolve(value) for value in self.input_dirs], input_dir)
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            env = dict(os.environ)
            env.update({
                "PIPELINE_INPUT_DIR": str(input_dir.resolve()),
                "PIPELINE_OUTPUT_DIR": str(output_dir.resolve()),
            })
            parameters = {
                str(key): value for key, value in self.parameters.items()
                if str(key).strip() and str(key) != "model_plan"
            }
            if parameters:
                env["PIPELINE_PARAMS_JSON"] = json.dumps(
                    parameters, ensure_ascii=False, sort_keys=True
                )
            for key, value in sorted(parameters.items()):
                env_name = "PIPELINE_PARAM_" + "".join(
                    char if char.isalnum() else "_" for char in key.upper()
                ).strip("_")
                if env_name != "PIPELINE_PARAM_":
                    env[env_name] = str(value)
                direct_name = _direct_parameter_environment_name(key)
                if direct_name:
                    env[direct_name] = str(value)
            for key, source_env_name in sorted(self.secret_environment.items()):
                target = "PIPELINE_PARAM_" + "".join(
                    char if char.isalnum() else "_" for char in str(key).upper()
                ).strip("_")
                value = os.environ.get(str(source_env_name), "")
                if not value:
                    raise RuntimeError(
                        f"Sensitive parameter {key!r} is not configured for {self.asset_key}."
                    )
                env[target] = value
                direct_name = _direct_parameter_environment_name(key)
                if direct_name:
                    env[direct_name] = value

            missing_required = []
            for requirement in self.runtime_environment:
                name = str(requirement.get("name") or "").strip()
                if not name or env.get(name) not in (None, ""):
                    continue
                if requirement.get("required"):
                    missing_required.append(name)
                else:
                    context.log.warning(
                        f"Optional runtime environment variable {name} is not set "
                        f"for {self.asset_key}."
                    )
            if missing_required:
                raise RuntimeError(
                    f"Node {self.asset_key} requires runtime environment variable(s): "
                    + ", ".join(sorted(missing_required))
                )

            started_at = time.monotonic()
            process = subprocess.Popen(
                [sys.executable, str(resolve(self.script_path)), *self.arguments],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            lines = queue.Queue()
            closed = object()
            recent = deque(maxlen=400)

            def read_output():
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        lines.put(line.rstrip())
                finally:
                    lines.put(closed)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            while True:
                try:
                    line = lines.get(timeout=15.0)
                except queue.Empty:
                    if process.poll() is None:
                        context.log.info(
                            f"Node {self.asset_key} is still running "
                            f"({time.monotonic() - started_at:.0f}s elapsed)."
                        )
                        continue
                    line = closed
                if line is closed:
                    break
                if line:
                    recent.append(line)
                    context.log.info(line)

            if process.wait() != 0:
                diagnostic = "\\n".join(recent)[-12000:]
                raise RuntimeError(
                    f"Node script {self.asset_key} failed with exit code "
                    f"{process.returncode}:\\n{diagnostic}"
                )
            reader.join(timeout=1.0)
            moved = (
                _normalize_single_output_port(output_dir, self.output_ports)
                if self.output_ports
                else []
            )
            if moved:
                context.log.warning(
                    "Task outputs were routed internally through the sole declared "
                    f"port {self.output_ports[0]!r}: {', '.join(moved)}"
                )
            artifacts = _artifacts(output_dir)
            return dg.MaterializeResult(metadata={
                "input_dir": str(input_dir),
                "output_dir": str(output_dir),
                "artifact_count": len(artifacts),
                "artifacts": dg.MetadataValue.json(artifacts),
            })

        return dg.Definitions(assets=[run_script])
'''
