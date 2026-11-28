"""Filesystem hand-off helpers of the exported Dagster component.

The text below the marker line is shipped in every bundle, as part of
``shell_command.py`` (see ``filesystem_runtime.filesystem_shell_component_source``).
It is a real module here so that tests and the control plane run the same
code that a bundle runs. The shipped component brings its own imports and the
artifact runtime, so the imports above the marker are for this module only.

Keep the part below the marker free of anything the component does not
import, and of type hints that need extra imports.
"""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from inlumen.contracts.artifact_runtime import atomic_json

SHIPPED_FROM = "# ---- Everything below this line is shipped in exported bundles, unchanged. ----"


def shipped_source() -> str:
    """The part of this file that goes into a bundle."""
    text = Path(__file__).read_text(encoding="utf-8")
    return text.split(SHIPPED_FROM + "\n", 1)[1]


# ---- Everything below this line is shipped in exported bundles, unchanged. ----
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


def _parameter_environment(parameters):
    """Environment variables that carry the configured parameters of a Task.

    A parameter named QUESTION is available as os.getenv("QUESTION"). The
    prefixed names remain available for older generated Task code.
    """
    normalized = {
        str(key): value for key, value in parameters.items()
        if str(key).strip() and str(key) not in {"model_plan", "output_artifacts"}
    }
    environment = {}
    if normalized:
        environment["PIPELINE_PARAMS_JSON"] = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True
        )
    for key, value in sorted(normalized.items()):
        prefixed_name = "PIPELINE_PARAM_" + "".join(
            char if char.isalnum() else "_" for char in key.upper()
        ).strip("_")
        if prefixed_name != "PIPELINE_PARAM_":
            environment[prefixed_name] = str(value)
        direct_name = _direct_parameter_environment_name(key)
        if direct_name:
            environment[direct_name] = str(value)
    return environment


def _same_file_contents(first, second):
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as first_handle, second.open("rb") as second_handle:
        while True:
            first_chunk = first_handle.read(1024 * 1024)
            second_chunk = second_handle.read(1024 * 1024)
            if first_chunk != second_chunk:
                return False
            if not first_chunk:
                return True


def _remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _publish_staged_directory(staging, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if destination.exists() or destination.is_symlink():
        previous = Path(tempfile.mkdtemp(
            prefix=f".{destination.name}.previous-",
            dir=destination.parent,
        ))
        previous.rmdir()
        os.replace(destination, previous)
    try:
        os.replace(staging, destination)
    except Exception:
        if previous is not None and (previous.exists() or previous.is_symlink()):
            os.replace(previous, destination)
        raise
    else:
        if previous is not None:
            _remove_path(previous)


def _stage_inputs(source_dirs, input_dir):
    input_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(
        prefix=f".{input_dir.name}.staging-",
        dir=input_dir.parent,
    ))
    owners = {}
    try:
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
                destination = staging_dir / relative
                existing = owners.get(relative)
                if existing is not None:
                    if _same_file_contents(source, existing):
                        continue
                    raise RuntimeError(
                        f"Upstream artifacts collide at {relative.as_posix()!r}; "
                        "add a Task that merges or renames them."
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                owners[relative] = source
        _publish_staged_directory(staging_dir, input_dir)
    except Exception:
        _remove_path(staging_dir)
        raise


def _stage_input_bindings(bindings, input_dir):
    input_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(
        prefix=f".{input_dir.name}.staging-",
        dir=input_dir.parent,
    ))
    owners = {}
    try:
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
                destination = staging_dir / relative
                existing = owners.get(relative)
                if existing is not None:
                    if _same_file_contents(source, existing):
                        continue
                    raise RuntimeError(
                        f"Upstream artifacts collide at {relative.as_posix()!r}; "
                        "add a Task that merges or renames them."
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                owners[relative] = source
        _publish_staged_directory(staging_dir, input_dir)
    except Exception:
        _remove_path(staging_dir)
        raise


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
            + ". With several output ports, write each result into its port "
            "folder or declare the outputs in inlumen.task.json."
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


STEP_RECEIPT = ".inlumen-step.json"


def _input_digest(input_dir):
    """Content digest of everything staged for a step, independent of timing."""
    digest = hashlib.sha256(b"inlumen.step-input@1\0")
    for item in _artifacts(Path(input_dir)) if Path(input_dir).is_dir() else []:
        digest.update(item["path"].encode("utf-8") + b"\0" + item["sha256"].encode() + b"\0")
    return digest.hexdigest()


def _step_cache_context(output_dir_attribute, input_dir):
    """Return this step's cache keys and, when staged, a reusable earlier result.

    The platform stages candidate results under INLUMEN_STEP_CACHE_DIR with a
    keys.json naming each step's code key; the input key is computed here,
    after inputs are staged.
    """
    root = os.environ.get("INLUMEN_STEP_CACHE_DIR", "").strip()
    if not root:
        return {}
    node_dir = Path(str(output_dir_attribute)).name
    try:
        keys = json.loads((Path(root) / "keys.json").read_text(encoding="utf-8")).get(node_dir) or {}
    except (OSError, ValueError):
        return {}
    if not keys.get("cacheable") or not keys.get("static_key"):
        return {}
    context = {"static_key": str(keys["static_key"]), "input_digest": _input_digest(input_dir), "cached": False}
    hit = Path(root) / node_dir / context["input_digest"] / "files"
    if keys.get("lookup") and hit.is_dir():
        context["hit"] = hit
    return context


def _write_step_receipt(root, asset_key, started_at, input_dir, bindings, artifacts, cache=None):
    """Record what a step read and published, for lineage and caching.

    The receipt sits next to the published ports; the platform reads it after
    the run and never hands it to downstream steps.
    """
    connections = [
        {key: str(item.get(key) or "") for key in (
            "source_node", "source_port", "connection_id", "target_port", "filename",
        )}
        for item in bindings or []
        if isinstance(item, dict)
    ]
    atomic_json(Path(root) / STEP_RECEIPT, {
        "schema_version": "inlumen.step-receipt@1",
        "asset_key": asset_key,
        "status": "succeeded",
        "started_at": started_at,
        "finished_at": time.time(),
        "connections": connections,
        "inputs": _artifacts(Path(input_dir)) if Path(input_dir).is_dir() else [],
        "outputs": [item for item in artifacts if item["path"] != STEP_RECEIPT],
        **{key: value for key, value in (cache or {}).items() if key != "hit"},
    })


def _validate_output_ports(output_dir, required_ports):
    for port in required_ports:
        root = output_dir / port
        if not any(
            path.is_file() and path.name not in {
                ".gitkeep", "input_manifest.json", "output_manifest.json"
            }
            for path in root.rglob("*")
        ):
            raise RuntimeError(
                f"Required output port {port!r} contains no artifacts. "
                "The node must write output files to PIPELINE_OUTPUT_DIR before completing."
            )


