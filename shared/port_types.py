"""Port types derived from artifact contracts.

A port type is a data kind, optionally followed by ``[]`` for a collection of
that kind: ``table``, ``table[]``, ``json``, ``any``, ``any[]``. Two extra
kinds describe directory artifacts whose items cannot be summarised by one
kind: ``bundle`` (mixed members, e.g. model + metrics) and ``directory`` (no
declared members).

The artifact contract is the source of truth; ports show what flows through
them. A port whose declared type is a wildcard takes the type inferred from
its node's files or artifact declarations. Mismatches are graded: a kind
mismatch is an error, a cardinality mismatch or an uncheckable bundle is a
warning. The rules live in ``port_types.json`` so the browser applies the
same ones (frontend/src/features/flow/portTypes.ts).

Canonical copy: shared/port_types.py (synced by scripts/sync_shared.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, NamedTuple

SPEC = json.loads(Path(__file__).with_name("port_types.json").read_text(encoding="utf-8"))
WILDCARDS = frozenset(SPEC["wildcards"])
OPAQUE_KINDS = frozenset(SPEC["opaque_kinds"])
EXTENSION_KINDS: dict[str, str] = dict(SPEC["extension_kinds"])

FILES_AS_PARAMETER = "files_as"
FILES_AS_COLLECTION = "collection"
FILES_AS_SEPARATE = "separate"


class PortType(NamedTuple):
    kind: str
    many: bool

    def __str__(self) -> str:
        return f"{self.kind}[]" if self.many else self.kind

    @property
    def is_wildcard(self) -> bool:
        return self.kind == "any"


class PortIssue(NamedTuple):
    severity: str
    code: str
    message: str


def parse_port_type(value: object) -> PortType:
    text = str(value or "").strip().lower()
    many = text.endswith("[]")
    kind = text[:-2].strip() if many else text
    return PortType("any" if kind in WILDCARDS else kind, many)


def kind_for_filename(filename: object) -> str:
    suffix = PurePosixPath(str(filename or "")).suffix.lstrip(".").lower()
    return EXTENSION_KINDS.get(suffix, "binary")


def _item_kind(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "").strip().lower()
    if kind in {"", "binary"}:
        guessed = kind_for_filename(item.get("filename") or item.get("path") or "")
        if guessed == "binary" and item.get("format"):
            guessed = kind_for_filename(f"x.{item['format']}")
        return guessed
    return kind


def collection_type(kinds: Iterable[str]) -> str:
    distinct = {kind for kind in kinds if kind}
    if not distinct:
        return "directory"
    if len(distinct) == 1:
        return f"{distinct.pop()}[]"
    return "bundle"


def artifact_port_type(artifact: dict[str, Any]) -> str:
    """The port type of one declared artifact (file, collection or bundle)."""
    is_directory = (
        str(artifact.get("representation") or "") == "directory"
        or str(artifact.get("kind") or "") == "directory"
    )
    if not is_directory:
        return _item_kind(artifact)
    members = [member for member in artifact.get("members") or [] if isinstance(member, dict)]
    return collection_type(_item_kind(member) for member in members)


def file_port_ids(filenames: Iterable[str]) -> list[str]:
    """Stable, unique output-port ids for a Source that publishes each file separately."""
    used: set[str] = set()
    ids = []
    for filename in filenames:
        stem = PurePosixPath(str(filename)).stem.lower()
        base = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_") or "file"
        if not base[0].isalpha():
            base = f"f_{base}"
        candidate, suffix = base, 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        ids.append(candidate)
    return ids


def files_as(parameters: dict[str, Any] | None) -> str:
    value = str((parameters or {}).get(FILES_AS_PARAMETER) or "").strip().lower()
    return FILES_AS_SEPARATE if value == FILES_AS_SEPARATE else FILES_AS_COLLECTION


def source_output_types(
    filenames: list[str], mode: str, port_ids: list[str]
) -> dict[str, str]:
    """Types of a file Source's outputs: one collection port, or one port per file."""
    if not filenames or not port_ids:
        return {}
    if mode == FILES_AS_SEPARATE:
        by_port = dict(zip(file_port_ids(filenames), filenames))
        return {port: kind_for_filename(by_port[port]) for port in port_ids if port in by_port}
    if len(filenames) == 1:
        return {port_ids[0]: kind_for_filename(filenames[0])}
    return {port_ids[0]: collection_type(kind_for_filename(name) for name in filenames)}


def declared_output_types(
    declarations: list[dict[str, Any]], port_ids: list[str]
) -> dict[str, str]:
    """Types of the outputs a node declares as artifacts, keyed by output port."""
    items = [item for item in declarations if isinstance(item, dict)]
    if not items or not port_ids:
        return {}
    if len(items) == 1 and not items[0].get("port"):
        return {port_ids[0]: artifact_port_type(items[0])}
    types = {}
    for item in items:
        port = str(item.get("port") or item.get("name") or "")
        if port in port_ids:
            types[port] = artifact_port_type(item)
    return types


def effective_port_type(declared: object, inferred: object | None) -> str:
    """A declared type wins; a wildcard port takes what its contract implies."""
    declared_type = parse_port_type(declared)
    if inferred is None or not declared_type.is_wildcard:
        return str(declared_type)
    inferred_type = parse_port_type(inferred)
    if declared_type.many and not inferred_type.many and inferred_type.kind not in OPAQUE_KINDS:
        return str(declared_type)
    return str(inferred_type)


def check_connection(source: object, target: object) -> PortIssue | None:
    """Grade a connection from an output of type ``source`` to an input of type ``target``."""
    left, right = parse_port_type(source), parse_port_type(target)
    if right.is_wildcard and not right.many:
        return None
    if left.is_wildcard and not left.many:
        return None
    if left.kind in OPAQUE_KINDS:
        if right.kind in OPAQUE_KINDS or right.is_wildcard:
            return None
        return PortIssue(
            "warning", "unchecked-bundle",
            f"A {left} arrives at a {right} input; its contents cannot be checked before running.",
        )
    if not left.is_wildcard and not right.is_wildcard and left.kind != right.kind:
        return PortIssue(
            "error", "incompatible-port-kinds",
            f"Port types are incompatible: {left} -> {right}.",
        )
    if left.many and not right.many:
        return PortIssue(
            "warning", "collection-into-single",
            f"A collection ({left}) arrives at a single {right} input and is delivered as one "
            "directory. Add a Parallel Map to process each item, or type the input as "
            f"{right.kind}[].",
        )
    if right.many and not left.many:
        return PortIssue(
            "warning", "single-into-collection",
            f"A single {left} arrives where a collection ({right}) is expected.",
        )
    return None


NON_DATA_FILENAMES = frozenset({
    "main.py", "requirements.txt", "node-manifest.json", "validation-report.json", "inlumen.task.json",
})


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def data_filenames(files: object) -> list[str]:
    """Names of the data files attached to a node (code and manifests excluded)."""
    names = []
    for item in files if isinstance(files, list) else []:
        if isinstance(item, dict):
            if str(item.get("role") or "") == "code":
                continue
            name = str(item.get("filename") or item.get("name") or "")
        else:
            name = str(item or "")
        base = PurePosixPath(name).name
        if base and base not in NON_DATA_FILENAMES and not base.endswith(".py"):
            names.append(base)
    return names


def artifact_declarations(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Output artifacts a node declares: authored first, then its generated contract."""
    parameters = _mapping(data.get("param") or data.get("parameters") or data.get("param_json"))
    implementation = _mapping(data.get("implementation"))
    for source in (implementation, parameters):
        plural = source.get("output_artifacts")
        if isinstance(plural, list) and plural:
            return [item for item in plural if isinstance(item, dict)]
        single = source.get("output_artifact")
        if isinstance(single, dict):
            return [single]
    generated = _mapping(data.get("generated_artifact"))
    outputs = _mapping(generated.get("data_contract")).get("outputs")
    return [item for item in outputs if isinstance(item, dict)] if isinstance(outputs, list) else []


def infer_output_types(kind: str, data: dict[str, Any], port_ids: list[str]) -> dict[str, str]:
    """Port types a node's outputs carry, inferred from its artifact contract or files."""
    if kind == "source":
        # Attached files are the Source's contract; a generated one may be stale.
        parameters = _mapping(data.get("param") or data.get("parameters") or data.get("param_json"))
        inferred = source_output_types(data_filenames(data.get("files")), files_as(parameters), port_ids)
        if inferred:
            return inferred
    return declared_output_types(artifact_declarations(data), port_ids)
