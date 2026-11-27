"""Engine-neutral filesystem hand-off used by the pipeline runtime.

This module deliberately knows nothing about Dagster, node schemas, or user
code.  A runner prepares a fresh ``input`` directory for each node, executes
the node with the two public environment variables, then inventories the
resulting output directory for the orchestration layer.

The hand-off itself lives in ``filesystem_handoff``, whose source is shipped
in exported bundles. The functions here are its typed entry points for the
control plane and the tests, so both run the code that a bundle runs.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterable

from inlumen.contracts import artifact_runtime_source

from export import filesystem_handoff as handoff


PIPELINE_INPUT_DIR = "PIPELINE_INPUT_DIR"
PIPELINE_OUTPUT_DIR = "PIPELINE_OUTPUT_DIR"


def direct_parameter_environment_name(key: object) -> str:
    """Return the direct Task environment name for a safe parameter key."""
    return handoff._direct_parameter_environment_name(key)


def parameter_environment(parameters: dict[object, object]) -> dict[str, str]:
    """Encode configured Task parameters for a process environment.

    A parameter named ``QUESTION`` is available as ``os.getenv("QUESTION")``.
    The prefixed aliases remain available for older generated Task code.
    """
    normalized = {
        str(key): value
        for key, value in parameters.items()
        if str(key).strip() and str(key) not in {"model_plan", "output_artifacts"}
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


def stage_input_directories(
    source_directories: Iterable[Path | str],
    input_dir: Path | str,
) -> Path:
    """Create a Task input directory from zero or more upstream directories.

    Files retain their relative paths.  Two upstream producers may not write
    different artifacts to the same path: silently picking one would make the
    pipeline nondeterministic.
    """
    destination = Path(input_dir)
    handoff._stage_inputs(source_directories, destination)
    return destination


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
    destination = Path(input_dir)
    handoff._stage_input_bindings(bindings, destination)
    return destination


def normalize_single_output_port(
    output_dir: Path | str,
    output_ports: Iterable[str],
) -> list[str]:
    """Move public root outputs into the orchestration-owned output port.

    Task code writes directly to ``PIPELINE_OUTPUT_DIR``.  For a Task with one
    output port, the runner namespaces those artifacts only after the process
    exits so downstream routing remains internal to the orchestrator.  A legacy
    Task with several output ports must write each result into its port folder.
    """
    return handoff._normalize_single_output_port(Path(output_dir), output_ports)


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


def validate_output_ports(output_dir: Path | str, required_ports: Iterable[str]) -> None:
    """Require an artifact from each required producer output before success."""
    handoff._validate_output_ports(Path(output_dir), required_ports)


def discover_artifacts(output_dir: Path | str) -> list[dict[str, object]]:
    """Inventory files produced by a Task for runtime/provenance use only.

    This metadata is generated *after* execution.  It is intentionally not an
    input/output schema and is never required from user code.
    """
    root = Path(output_dir)
    return handoff._artifacts(root) if root.is_dir() else []


def filesystem_shell_component_source() -> str:
    """Return the self-contained Dagster component shipped in an export.

    Generated bundles must run independently of the control-plane package, so
    the runtime is emitted as source: the artifact runtime, the component's
    imports, the hand-off helpers of ``filesystem_handoff`` and the component.
    """
    return (
        artifact_runtime_source() + '\n'
        + _COMPONENT_IMPORTS + handoff.shipped_source() + _SHELL_COMMAND_SOURCE
    )


_COMPONENT_IMPORTS = '''import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import dagster as dg


'''


_SHELL_COMMAND_SOURCE = r'''class ShellCommand(dg.Component, dg.Model, dg.Resolvable):
    asset_key: str
    data_contract: dict = {}
    artifact_bindings: list[dict] = []
    script_path: str
    upstream_assets: list[str] = []
    input_dirs: list[str] = []
    input_bindings: list[dict] = []
    input_dir: str
    output_dir: str
    output_ports: list[str] = []
    required_output_ports: list[str] | None = None
    arguments: list[str] = []
    parameters: dict = {}
    secret_environment: dict = {}
    runtime_environment: list[dict] = []

    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        deps = [dg.AssetKey(asset_key) for asset_key in self.upstream_assets]

        @dg.asset(name=self.asset_key, deps=deps)
        def run_script(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
            project_root = Path.cwd()
            started_at = time.time()

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
            strict = self.data_contract.get("contract_id") == CONTRACT_ID
            if strict and output_dir.exists():
                shutil.rmtree(output_dir)
            bound_inputs = []
            if strict and self.artifact_bindings:
                bound_bindings = []
                for raw in self.artifact_bindings:
                    binding = dict(raw)
                    root = resolve(binding["source_dir"])
                    if binding.get("run_scoped", True):
                        root = root / run_scope
                    if binding.get("source_port"):
                        root = root / binding["source_port"]
                    binding["source_dir"] = str(root)
                    bound_bindings.append(binding)
                bound_inputs = stage_bound_artifacts(bound_bindings, input_dir)
            elif bindings:
                _stage_input_bindings(bindings, input_dir)
            else:
                _stage_inputs([resolve(value) for value in self.input_dirs], input_dir)
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            published_dir = output_dir
            cache = _step_cache_context(self.output_dir, input_dir)
            if cache.get("hit") is not None:
                # Same step code, parameters and input content as an earlier
                # successful run: reuse its published outputs instead of running.
                staged = Path(tempfile.mkdtemp(dir=published_dir.parent, prefix=".cache-"))
                staged.rmdir()
                shutil.copytree(cache.pop("hit"), staged)
                publish_artifact_directory(staged, published_dir)
                context.log.info(f"INLUMEN_CACHE_HIT {self.asset_key}")
                artifacts = _artifacts(published_dir)
                _write_step_receipt(published_dir, self.asset_key, started_at, input_dir,
                                    bound_inputs or bindings, artifacts, {**cache, "cached": True})
                return dg.MaterializeResult(metadata={
                    "input_dir": str(input_dir), "output_dir": str(published_dir), "cached": True,
                    "artifact_count": len(artifacts), "artifacts": dg.MetadataValue.json(artifacts),
                })
            work_dir = input_dir.parent.parent / "runtime" / run_scope
            if strict:
                if work_dir.exists():
                    shutil.rmtree(work_dir)
                work_dir.mkdir(parents=True, exist_ok=True)
                output_dir = work_dir / "publish"
                output_dir.mkdir()
                if not self.artifact_bindings:
                    bound_inputs = [validate_artifact(input_dir, item) for item in self.data_contract.get("inputs", [])]
            env = dict(os.environ)
            if strict:
                env.update(prepare_node_environment(input_dir, output_dir, work_dir,
                    bound_inputs, self.data_contract, self.parameters))
            env.update({
                "PIPELINE_INPUT_DIR": str(input_dir.resolve()),
                "PIPELINE_OUTPUT_DIR": str(output_dir.resolve()),
            })
            parameters = {
                str(key): value for key, value in self.parameters.items()
                if str(key).strip() and str(key) not in {"model_plan", "output_artifacts"}
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

            if strict:
                context.log.info("Artifact input bindings: " + json.dumps([
                    {key: item.get(key) for key in ("source_node", "connection_id", "filename")}
                    for item in bound_inputs
                ], sort_keys=True))
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
            if strict:
                declarations = self.data_contract.get("outputs", [])
                receipt = work_dir / "outputs.json"
                actual = json.loads(receipt.read_text()).get("outputs") if receipt.is_file() else None
                validated = (
                    validate_result(output_dir, declarations, actual, producer=self.asset_key)
                    if declarations or self.output_ports
                    else []
                )
                # Each declared output is published under its own port.
                publish_port_artifacts(
                    output_dir,
                    validated,
                    published_dir,
                    self.output_ports[0] if self.output_ports else "result",
                )
                artifacts = _artifacts(published_dir)
                _write_step_receipt(published_dir, self.asset_key, started_at, input_dir,
                                    bound_inputs, artifacts, cache)
                return dg.MaterializeResult(metadata={
                    "input_dir": str(input_dir), "output_dir": str(published_dir),
                    "artifact_count": len(declarations), "artifacts": dg.MetadataValue.json(artifacts),
                })
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
            _validate_output_ports(
                output_dir,
                self.output_ports if self.required_output_ports is None else self.required_output_ports,
            )
            artifacts = _artifacts(output_dir)
            _write_step_receipt(output_dir, self.asset_key, started_at, input_dir,
                                bindings, artifacts, cache)
            return dg.MaterializeResult(metadata={
                "input_dir": str(input_dir),
                "output_dir": str(output_dir),
                "artifact_count": len(artifacts),
                "artifacts": dg.MetadataValue.json(artifacts),
            })

        return dg.Definitions(assets=[run_script])
'''
