"""Version 4 artifact boundary, shared by codegen and exported runtimes.

Only declared artifacts are data. Directory bundles are never flattened into
descriptors. Receipts and scratch space live outside the publication directory.
"""

import csv
import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

CONTRACT_ID = "inlumen.generic-node@2"


class ArtifactContractError(ValueError):
    pass


def relative_path(value):
    value = str(value or "")
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
    ):
        raise ArtifactContractError(f"Unsafe artifact-relative path: {value!r}")
    return path.as_posix()


def validate_declaration(declaration):
    if not isinstance(declaration, dict) or not declaration.get("name"):
        raise ArtifactContractError(
            "Artifact requires a logical name and relative filename."
        )
    relative_path(declaration.get("filename"))
    representation = declaration.get("representation") or (
        "directory" if declaration.get("kind") == "directory" else "file"
    )
    if representation not in {"file", "directory"}:
        raise ArtifactContractError(
            f"Unsupported artifact representation: {representation}"
        )
    identity = {
        key: str(declaration[key])
        for key in ("name", "source_node", "connection_id", "target_port")
        if declaration.get(key) is not None
    }
    return {**declaration, **identity, "representation": representation}


def validate_connection_contract(produced, required, *, connection=""):
    """Reject provable conflicts; actual content is also checked at the boundary."""
    for key in ("representation", "format"):
        if produced.get(key) and required.get(key) and produced[key] != required[key]:
            raise ArtifactContractError(
                f"Connection {connection}: {key} mismatch: {produced[key]!r} -> {required[key]!r}"
            )

    def check_schema(source, target, location="$", required_property=True):
        if not isinstance(source, dict) or not isinstance(target, dict):
            return

        def types(schema):
            value = schema.get("type", [])
            values = {value} if isinstance(value, str) else set(value)
            if "number" in values:
                values.add("integer")
            return values

        left, right = types(source), types(target)
        if required_property and left and right and not left & right:
            raise ArtifactContractError(
                f"Connection {connection}: incompatible schema types at {location}: {sorted(left)} -> {sorted(right)}"
            )
        if required_property and "enum" in source and "enum" in target:
            if not any(item in target["enum"] for item in source["enum"]):
                raise ArtifactContractError(
                    f"Connection {connection}: incompatible schema values at {location}"
                )
        properties = source.get("properties", {})
        for name in target.get("required", []):
            if name not in properties and source.get("additionalProperties") is False:
                raise ArtifactContractError(
                    f"Connection {connection}: required property {location}.{name} is forbidden by the producer"
                )
            check_schema(
                properties.get(name),
                target.get("properties", {}).get(name),
                f"{location}.{name}",
            )

    check_schema(produced.get("schema", {}), required.get("schema", {}))


def _contained(path, root):
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (ValueError, OSError, RuntimeError) as exc:
        raise ArtifactContractError(
            f"Artifact path is missing or escapes its root: {path}"
        ) from exc


def validate_artifact(root, declaration, *, exclusive=False):
    declaration = validate_declaration(declaration)
    root = Path(root)
    path = root / relative_path(declaration["filename"])
    _contained(path, root)
    directory = declaration["representation"] == "directory"
    if not (path.is_dir() if directory else path.is_file()):
        raise ArtifactContractError(
            f"Artifact {declaration['name']!r} must be a {declaration['representation']}: {path}"
        )
    # Links may refer within the artifact, never to input/work directories.
    artifact_root = path if directory else path.parent
    if path.is_symlink():
        raise ArtifactContractError(f"Artifact root must not be a symlink: {path}")
    if directory:
        for entry in path.rglob("*"):
            _contained(entry, artifact_root)
            if entry.is_symlink() and entry.is_dir():
                raise ArtifactContractError(
                    f"Directory symlinks are not supported in bundles: {entry}"
                )
    if exclusive:
        for entry in root.rglob("*"):
            if entry == path or path in entry.parents or entry in path.parents:
                continue
            raise ArtifactContractError(
                f"Undeclared output {entry.relative_to(root)}; expected only {declaration['filename']!r}"
            )
    if directory:
        members = declaration.get("members") or []
        for member in members:
            validate_artifact(path, member)
        if members:
            allowed = [path / relative_path(member["filename"]) for member in members]
            for entry in path.rglob("*"):
                if not any(
                    entry == member
                    or member in entry.parents
                    or entry in member.parents
                    for member in allowed
                ):
                    raise ArtifactContractError(
                        f"Undeclared bundle member: {entry.relative_to(path)}"
                    )
    schema = declaration.get("schema") or {}
    if not directory and (
        declaration.get("format") == "json" or declaration.get("kind") == "json"
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ArtifactContractError(
                f"Artifact {declaration['name']!r} is not valid JSON: {exc}"
            ) from exc
        if schema:
            from jsonschema import Draft202012Validator

            Draft202012Validator.check_schema(schema)
            errors = sorted(
                Draft202012Validator(schema).iter_errors(value),
                key=lambda e: str(e.path),
            )
            if errors:
                raise ArtifactContractError(
                    f"Artifact {declaration['name']!r} violates its schema: {errors[0].message}"
                )
    if not directory and declaration.get("format") == "csv":
        with path.open(encoding="utf-8", newline="") as stream:
            columns = next(csv.reader(stream), [])
        missing = set(declaration.get("required_columns") or []) - set(columns)
        if missing:
            raise ArtifactContractError(
                f"Artifact {declaration['name']!r} is missing columns: {sorted(missing)}"
            )
    return {**declaration, "path": str(path.resolve())}


def validate_result(root, declarations, actual=None, *, producer=""):
    try:
        if len(declarations) != 1:
            raise ArtifactContractError(
                f"Expected exactly one declared output artifact, got {len(declarations)}"
            )
        expected = validate_declaration(declarations[0])
        if actual is not None:
            if (
                not isinstance(actual, list)
                or len(actual) != 1
                or not isinstance(actual[0], dict)
            ):
                raise ArtifactContractError(
                    "Expected exactly one returned artifact descriptor"
                )
            item = actual[0]
            for field in ("name", "filename", "kind", "format", "representation"):
                if (
                    item.get(field)
                    and expected.get(field)
                    and item[field] != expected[field]
                ):
                    raise ArtifactContractError(
                        f"Returned artifact {field} {item[field]!r} does not match {expected[field]!r}"
                    )
            if item.get("path"):
                returned = Path(item["path"])
                if not returned.is_absolute():
                    returned = Path(root) / returned
                if returned.resolve() != (Path(root) / expected["filename"]).resolve():
                    raise ArtifactContractError(
                        "Returned artifact path does not match the declared output"
                    )
        return [validate_artifact(root, expected, exclusive=True)]
    except ArtifactContractError as exc:
        raise ArtifactContractError(
            f"Artifact contract violation at {producer or 'producer'}: {exc}"
        ) from exc


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".receipt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def publish_artifact_directory(staging, destination):
    """Replace a complete publication, retaining the previous one on rename failure."""
    staging, destination = Path(staging), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Docker can mount workspaces and outputs on different filesystems. The
    # final rename must use a staging directory on the publication filesystem.
    # Bind mounts can report the same st_dev yet still reject cross-mount
    # renames. Always stage next to the destination, not merely on its device.
    if staging.parent.resolve() != destination.parent.resolve():
        local = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".publication-"))
        try:
            shutil.copytree(staging, local, dirs_exist_ok=True)
            publish_artifact_directory(local, destination)
        finally:
            if local.exists():
                shutil.rmtree(local)
        shutil.rmtree(staging)
        return
    previous = None
    if destination.exists() or destination.is_symlink():
        previous = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".previous-"))
        previous.rmdir()
        os.replace(destination, previous)
    try:
        os.replace(staging, destination)
    except OSError:
        if previous is not None:
            os.replace(previous, destination)
        raise
    else:
        if previous is not None:
            if previous.is_symlink() or previous.is_file():
                previous.unlink()
            else:
                shutil.rmtree(previous)


def stage_bound_artifacts(bindings, destination):
    """Bindings contain a publication root and exactly one declared artifact."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".artifacts-"))
    descriptors = []
    occupied = set()
    try:
        for binding in bindings:
            declaration = binding["artifact"]
            try:
                source = validate_artifact(binding["source_dir"], declaration)
                relative = relative_path(
                    binding.get("filename") or declaration["filename"]
                )
                if any(
                    relative == p
                    or relative.startswith(p + "/")
                    or p.startswith(relative + "/")
                    for p in occupied
                ):
                    raise ArtifactContractError(
                        f"Bound artifacts collide at {relative!r}; declare distinct input paths"
                    )
                occupied.add(relative)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if source["representation"] == "directory":
                    shutil.copytree(source["path"], target)
                else:
                    shutil.copy2(source["path"], target)
                descriptors.append(
                    {
                        **source,
                        "filename": relative,
                        "path": str((destination / relative).resolve()),
                        "source_node": str(binding.get("source_node", "")),
                        "connection_id": str(binding.get("connection_id", "")),
                        "target_port": str(binding.get("target_port", "")),
                    }
                )
            except ArtifactContractError as exc:
                raise ArtifactContractError(
                    f"Connection {binding.get('connection_id', '?')} from {binding.get('source_node', '?')}: {exc}"
                ) from exc
        publish_artifact_directory(staging, destination)
        return descriptors
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def prepare_node_environment(
    input_dir, output_dir, work_dir, inputs, contract, parameters=None
):
    resolved_inputs = list(contract.get("inputs", []))
    for requirement in contract.get("input_requirements", []):
        matches = [item for item in inputs if str(item.get("target_port")) == str(requirement["target_port"])]
        if not matches:
            raise ArtifactContractError(f"Required input port {requirement['target_port']} has no bound artifact")
        for item in matches:
            resolved_inputs.append({**item, **requirement, "filename": item["filename"], "connection_id": item["connection_id"]})
    contract = {**contract, "inputs": resolved_inputs}
    # Component engines can coerce numeric-looking strings inside nested dicts.
    # Identity is always textual, independently of the orchestration loader.
    contract = {
        **contract,
        "inputs": [
            validate_declaration(
                {
                    **item,
                    "name": item.get("name") or Path(item.get("filename", "")).stem,
                }
            )
            for item in contract.get("inputs", [])
        ],
        "outputs": [validate_declaration(item) for item in contract.get("outputs", [])],
    }
    for expected in contract.get("inputs", []):
        connection_id = expected.get("connection_id")
        if not connection_id:
            continue
        matches = [
            item for item in inputs if item.get("connection_id") == connection_id
        ]
        if len(matches) != 1:
            raise ArtifactContractError(
                f"Connection {connection_id}: expected exactly one bound artifact, got {len(matches)}"
            )
        validate_connection_contract(matches[0], expected, connection=connection_id)
        try:
            validate_artifact(
                input_dir,
                {**matches[0], **expected, "filename": matches[0]["filename"]},
            )
        except ArtifactContractError as exc:
            raise ArtifactContractError(
                f"Connection {connection_id} from {matches[0].get('source_node', '?')}, expected {expected.get('filename')}: {exc}"
            ) from exc
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    atomic_json(work_dir / "inputs.json", {"inputs": inputs})
    atomic_json(
        work_dir / "context.json",
        {"data_contract": contract, "parameters": parameters or {}},
    )
    return {
        "PIPELINE_INPUT_DIR": str(Path(input_dir).resolve()),
        "PIPELINE_OUTPUT_DIR": str(Path(output_dir).resolve()),
        "PIPELINE_WORK_DIR": str(work_dir.resolve()),
        "INLUMEN_INPUT_MANIFEST": str((work_dir / "inputs.json").resolve()),
        "INLUMEN_OUTPUT_MANIFEST": str((work_dir / "outputs.json").resolve()),
        "INLUMEN_CONTEXT_PATH": str((work_dir / "context.json").resolve()),
        "INLUMEN_PARAMS_JSON": json.dumps(parameters or {}),
    }
