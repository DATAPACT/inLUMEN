"""Build a pipeline design from a deployment artifact (issue #98).

Two artifacts are read, both without a model:

- an inLUMEN deployment bundle (the ZIP, or its ``bundle-manifest.json``),
  whose manifest lists the steps, their ports and the connections;
- an Argo Workflows file of any origin, whose tasks and dependencies become
  steps and connections.

The result is a Project JSON document, which the canvas already imports, and
a list of plain-language notes on what the artifact did not say. Nothing is
executed and no file of the artifact is written anywhere.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from collections import defaultdict
from typing import Any

from inlumen.contracts.node_ports import normalize_node_ports, ports_for_template
from inlumen.contracts.pipeline_graph_validation import (
    FLOW_BEHAVIORS,
    FLOW_EXPRESSION_PATTERN,
    validate_pipeline_graph,
    validation_issue_messages,
)
from inlumen.contracts.project_ir import project_document

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_STEPS = 500
BUNDLE_MANIFEST = "bundle-manifest.json"
BUNDLE_SCHEMA_PREFIX = "inlumen.deployment-bundle@"
ARGO_KINDS = {"Workflow", "WorkflowTemplate", "ClusterWorkflowTemplate", "CronWorkflow"}
_STEP_KINDS = {"source", "task", "destination", "flow", "subpipeline"}

# A ZIP of code: which files the model may read, and how much of them.
CODE_SUFFIXES = {".py", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".sh", ".yaml", ".yml", ".json", ".sql", ".r"}
CODE_NAMES = {"dockerfile", "makefile", "readme", "snakefile", "nextflow.config"}
TABLE_SUFFIXES = {".csv", ".tsv"}
SKIPPED_FOLDERS = {"__pycache__", "node_modules", "site-packages", "dist", "build", "__macosx"}
MAX_CODE_FILE_CHARS = 60_000
MAX_CODE_TOTAL_CHARS = 200_000
MAX_CODE_FILES = 60
MAX_ZIP_ENTRIES = 4096
MAX_TABLE_HEADER_CHARS = 400
MAX_SOURCE_FILE_BYTES = 50 * 1024 * 1024


class ArtifactError(ValueError):
    """The file is not an artifact this module can read; the message is for the user."""


# ----------------------------------------------------------------- shared

COLUMN_WIDTH = 340.0
ROW_HEIGHT = 200.0


def _layout(ids: list[str], parents: dict[str, set[str]]) -> dict[str, dict[str, float]]:
    """Left-to-right positions without overlap.

    A step sits one column after its last parent. Inside a column the steps
    follow the rows of their parents, so connections cross as little as a
    simple layout allows, and each step gets a row of its own.
    """
    depth: dict[str, int] = {}

    def level(node: str, trail: frozenset[str] = frozenset()) -> int:
        if node in depth:
            return depth[node]
        if node in trail:  # a cycle; the validation notes report it
            return 0
        known = [parent for parent in parents.get(node, ()) if parent in ids]
        depth[node] = 1 + max((level(parent, trail | {node}) for parent in known), default=-1)
        return depth[node]

    columns: dict[int, list[str]] = defaultdict(list)
    for node in ids:
        columns[level(node)].append(node)
    rows: dict[str, float] = {}
    positions = {}
    for column in sorted(columns):
        members = columns[column]

        def parent_row(node: str) -> float:
            known = [rows[parent] for parent in parents.get(node, ()) if parent in rows]
            return sum(known) / len(known) if known else float(members.index(node))

        ordered = sorted(members, key=lambda node: (parent_row(node), members.index(node)))
        for index, node in enumerate(ordered):
            rows[node] = index - (len(ordered) - 1) / 2
            positions[node] = {"x": 150.0 + COLUMN_WIDTH * column, "y": 300.0 + ROW_HEIGHT * rows[node]}
    return positions


def _relayout(graph: dict) -> None:
    """Give every step of a canvas graph a position from :func:`_layout`."""
    ids = [str(node.get("id")) for node in graph.get("nodes") or []]
    parents: dict[str, set[str]] = defaultdict(set)
    for edge in graph.get("edges") or []:
        parents[str(edge.get("target"))].add(str(edge.get("source")))
    positions = _layout(ids, parents)
    for node in graph.get("nodes") or []:
        node["position"] = positions[str(node.get("id"))]


def _result(kind: str, graph: dict, name: str, description: str, notes: list[str]) -> dict[str, Any]:
    report = validate_pipeline_graph(graph) if graph["nodes"] else {"issues": []}
    for message in validation_issue_messages(report):
        notes.append(f"The design does not pass validation yet: {message}")
    return {
        "kind": kind,
        "project": project_document(graph, name=name, description=description),
        "notes": notes,
        "step_count": len(graph["nodes"]),
        "connection_count": len(graph["edges"]),
    }


def _edge(source: str, source_port: str, target: str, target_port: str) -> dict[str, str]:
    return {
        "id": f"{source}:{source_port}->{target}:{target_port}",
        "source": source, "sourceHandle": source_port,
        "target": target, "targetHandle": target_port,
    }


# ----------------------------------------------------------------- bundle

def design_from_bundle_manifest(manifest: Any, *, name: str = "", run_spec: Any = None) -> dict[str, Any]:
    """The design recorded in an inLUMEN ``bundle-manifest.json``.

    ``run_spec`` is the bundle's ``run-spec.json`` when it is at hand: it adds
    the parameters of each step. The values of sensitive parameters are never
    in a bundle; their names are reported in a note.
    """
    if not isinstance(manifest, dict) or not str(manifest.get("schema_version") or "").startswith(BUNDLE_SCHEMA_PREFIX):
        raise ArtifactError("This is not an inLUMEN bundle manifest (schema_version inlumen.deployment-bundle).")
    raw_nodes = [node for node in manifest.get("nodes") or [] if isinstance(node, dict) and node.get("flow_id")]
    if not raw_nodes:
        raise ArtifactError("The bundle manifest lists no steps.")
    if len(raw_nodes) > MAX_STEPS:
        raise ArtifactError(f"The bundle has more than {MAX_STEPS} steps.")
    order = [str(item) for item in manifest.get("node_order") or []]
    raw_nodes.sort(key=lambda node: order.index(str(node["flow_id"])) if str(node["flow_id"]) in order else len(order))
    ids = [str(node["flow_id"]) for node in raw_nodes]
    if len(set(ids)) != len(ids):
        raise ArtifactError("The bundle manifest lists a step id twice.")

    edges, parents, dropped = [], defaultdict(set), 0
    for connection in manifest.get("connections") or []:
        if not isinstance(connection, dict):
            continue
        source, target = str(connection.get("source") or ""), str(connection.get("target") or "")
        if source not in ids or target not in ids:
            dropped += 1
            continue
        edges.append(_edge(source, str(connection.get("source_port") or ""), target, str(connection.get("target_port") or "")))
        parents[target].add(source)

    parameters: dict[str, dict] = {}
    secrets: dict[str, list[str]] = {}
    for item in (run_spec.get("nodes") if isinstance(run_spec, dict) else None) or []:
        if not isinstance(item, dict) or str(item.get("id")) not in ids:
            continue
        if isinstance(item.get("parameters"), dict) and item["parameters"]:
            parameters[str(item["id"])] = dict(item["parameters"])
        names = [str(name) for name in item.get("secret_parameters") or [] if isinstance(name, (str, int))]
        if names:
            secrets[str(item["id"])] = names

    positions = _layout(ids, parents)
    nodes, unknown_kinds = [], []
    for node in raw_nodes:
        flow_id = str(node["flow_id"])
        kind = str(node.get("type") or "").strip().lower()
        if kind not in _STEP_KINDS:
            unknown_kinds.append(str(node.get("label") or flow_id))
            kind = "task"
        data: dict[str, Any] = {
            "type": kind,
            "label": str(node.get("label") or flow_id),
            "description": str(node.get("description") or ""),
            "ports": normalize_node_ports(node.get("ports"), kind),
        }
        if node.get("template"):
            data["template_label"] = str(node["template"])
        if parameters.get(flow_id):
            data["param"] = parameters[flow_id]
        nodes.append({"id": flow_id, "type": "custom", "position": positions[flow_id], "data": data})

    labels = {str(node["flow_id"]): str(node.get("label") or node["flow_id"]) for node in raw_nodes}
    notes = [
        "The design was rebuilt from the bundle: steps, port names and types, and connections"
        + (", with the parameters of the steps." if isinstance(run_spec, dict) else "."),
        "Step descriptions are not in a bundle. The code of the Tasks and the data of the Sources are "
        "in the bundle ZIP, under nodes/ and inputs/.",
    ]
    if secrets:
        notes.append(
            "Sensitive parameters are not in a bundle. Enter them again for: "
            + "; ".join(f"{labels[key]} ({', '.join(names)})" for key, names in secrets.items()) + "."
        )
    if dropped:
        notes.append(f"{dropped} connection(s) named a step that is not in the manifest and were left out.")
    if unknown_kinds:
        notes.append("Steps of an unknown type were imported as Tasks: " + ", ".join(unknown_kinds) + ".")
    targets = [engine for engine, used in (manifest.get("targets") or {}).items() if used]
    description = "Imported from an inLUMEN deployment bundle" + (f" ({', '.join(sorted(targets))})." if targets else ".")
    return _result("inlumen-bundle", {"nodes": nodes, "edges": edges}, name or "Imported bundle", description, notes)


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ArtifactError("The ZIP file cannot be read.") from None
    if len(archive.infolist()) > MAX_ZIP_ENTRIES:
        raise ArtifactError(f"The ZIP has more than {MAX_ZIP_ENTRIES} entries.")
    return archive


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """At most ``limit`` bytes of one member; nothing is written to disk."""
    if info.flag_bits & 0x1:
        raise ArtifactError("The ZIP is encrypted.")
    with archive.open(info) as handle:
        return handle.read(limit)


def _member_path(info: zipfile.ZipInfo) -> str:
    return info.filename.replace("\\", "/")


def _run_spec_from_zip(archive: zipfile.ZipFile) -> Any | None:
    """The ``run-spec.json`` beside the bundle manifest, or None."""
    manifests = [
        _member_path(info) for info in archive.infolist()
        if not info.is_dir() and _member_path(info).split("/")[-1] == BUNDLE_MANIFEST and _member_path(info).count("/") <= 1
    ]
    if not manifests:
        return None
    folder = min(manifests, key=lambda path: path.count("/")).rsplit(BUNDLE_MANIFEST, 1)[0]
    for info in archive.infolist():
        if _member_path(info) == folder + "run-spec.json" and info.file_size <= MAX_DOCUMENT_BYTES:
            try:
                return json.loads(_read_member(archive, info, MAX_DOCUMENT_BYTES).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None
    return None


def _manifest_from_zip(archive: zipfile.ZipFile) -> Any | None:
    """The bundle manifest of a ZIP, or None when it has none at its top level."""
    candidates = [
        info for info in archive.infolist()
        if not info.is_dir()
        and _member_path(info).split("/")[-1] == BUNDLE_MANIFEST
        and _member_path(info).count("/") <= 1
    ]
    if not candidates:
        return None
    info = min(candidates, key=lambda item: item.filename.count("/"))
    if info.file_size > MAX_DOCUMENT_BYTES:
        raise ArtifactError("The bundle manifest is too large.")
    raw = _read_member(archive, info, MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ArtifactError("The bundle manifest is too large.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ArtifactError("The bundle manifest is not valid JSON.") from None


def _argo_in_zip(archive: zipfile.ZipFile) -> tuple[str, dict, int] | None:
    """The first Argo workflow file of a ZIP: (path, document, number of others)."""
    import yaml

    found: list[tuple[str, dict]] = []
    for info in sorted(archive.infolist(), key=_member_path):
        path = _member_path(info)
        if info.is_dir() or not path.lower().endswith((".yaml", ".yml")) or info.file_size > MAX_DOCUMENT_BYTES:
            continue
        try:
            documents = list(yaml.safe_load_all(_read_member(archive, info, MAX_DOCUMENT_BYTES).decode("utf-8")))
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        workflows = [item for item in documents if isinstance(item, dict) and item.get("kind") in ARGO_KINDS]
        if workflows:
            found.append((path, workflows[0]))
    return (found[0][0], found[0][1], len(found) - 1) if found else None


def code_files_from_zip(archive: zipfile.ZipFile) -> dict[str, Any]:
    """Which files of a code ZIP the model would read, and which it would not.

    Code and documentation are read whole, up to a size limit. Of a table
    (CSV, TSV) only the first line is read, for the column names. Every other
    file is listed by name only.
    """
    send: list[dict[str, Any]] = []
    listed: list[dict[str, Any]] = []
    total = 0
    for info in sorted(archive.infolist(), key=_member_path):
        path = _member_path(info)
        parts = path.split("/")
        name = parts[-1]
        if info.is_dir() or not name:
            continue
        lowered = name.lower()
        suffix = "." + lowered.rsplit(".", 1)[1] if "." in lowered else ""
        hidden = any(part.startswith(".") or part.lower() in SKIPPED_FOLDERS for part in parts)
        is_code = suffix in CODE_SUFFIXES or lowered in CODE_NAMES or lowered.split(".")[0] in CODE_NAMES
        is_table = suffix in TABLE_SUFFIXES
        if hidden or not (is_code or is_table) or len(send) >= MAX_CODE_FILES:
            listed.append({"path": path, "size_bytes": info.file_size})
            continue
        if is_table:
            raw = _read_member(archive, info, MAX_TABLE_HEADER_CHARS * 4)
            text = raw.decode("utf-8", "replace").splitlines()[0][:MAX_TABLE_HEADER_CHARS] if raw else ""
            part = "first line only"
        else:
            if info.file_size > MAX_CODE_FILE_CHARS * 4:
                listed.append({"path": path, "size_bytes": info.file_size})
                continue
            raw = _read_member(archive, info, MAX_CODE_FILE_CHARS * 4)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                listed.append({"path": path, "size_bytes": info.file_size})
                continue
            part = "whole file"
            if len(text) > MAX_CODE_FILE_CHARS:
                text, part = text[:MAX_CODE_FILE_CHARS], f"first {MAX_CODE_FILE_CHARS} characters"
        if not text.strip() or total + len(text) > MAX_CODE_TOTAL_CHARS:
            listed.append({"path": path, "size_bytes": info.file_size})
            continue
        total += len(text)
        send.append({"path": path, "part": part, "chars": len(text), "text": text})
    has_code = any(item["part"] != "first line only" and item["path"].lower().endswith((".py", ".r", ".sql", ".sh")) for item in send)
    return {"send": send, "listed": listed, "total_chars": total, "has_code": has_code}


# ------------------------------------------------------------------- argo

_TASK_REFERENCE = re.compile(
    r"\{\{\s*(?:tasks|steps)\.([A-Za-z0-9_.-]+?)\.outputs\.(?:(artifacts|parameters)\.([A-Za-z0-9_.-]+)|result)\b"
)
_DEPENDS_SUFFIX = re.compile(
    r"\.(Succeeded|Failed|Errored|Skipped|Omitted|Daemoned|AnySucceeded|AllFailed)\b"
)


def _humanize(name: str) -> str:
    words = re.sub(r"[-_.]+", " ", name).strip()
    return words[:1].upper() + words[1:] if words else name


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return slug or fallback


def _references(value: Any) -> list[tuple[str, str, str]]:
    """Every ``{{tasks.X.outputs...}}`` in a value: (task, argument name, output name)."""
    found: list[tuple[str, str, str]] = []

    def walk(item: Any, argument: str) -> None:
        if isinstance(item, dict):
            label = str(item.get("name") or argument) if ("from" in item or "value" in item) else argument
            for child in item.values():
                walk(child, label)
        elif isinstance(item, list):
            for child in item:
                walk(child, argument)
        elif isinstance(item, str):
            for match in _TASK_REFERENCE.finditer(item):
                found.append((match.group(1), argument, match.group(3) or "result"))

    walk(value, "")
    return found


def _what_runs(template: dict | None) -> str:
    """One sentence on what a leaf template runs, from its own fields only."""
    if not isinstance(template, dict):
        return ""
    for field in ("container", "script"):
        body = template.get(field)
        if isinstance(body, dict):
            parts = []
            if body.get("image"):
                parts.append(f"image {body['image']}")
            command = [str(item) for item in (body.get("command") or [])[:4]]
            if command:
                parts.append("command " + " ".join(command)[:120])
            return f"Runs a {field} (" + ", ".join(parts) + ")." if parts else f"Runs a {field}."
    for field, text in (
        ("resource", "Creates or changes a Kubernetes resource."),
        ("suspend", "Pauses the workflow."),
        ("http", "Calls an HTTP endpoint."),
        ("plugin", "Runs an executor plugin."),
        ("containerSet", "Runs a set of containers."),
        ("data", "Transforms data."),
    ):
        if field in template:
            return text
    return ""


_LOOP_KEYS = ("withItems", "withParam", "withSequence")
_WHEN_COMPARISON = re.compile(
    r"^\s*\{\{\s*(?:tasks|steps)\.[A-Za-z0-9_.-]+?\.outputs\.(?:result|parameters\.([A-Za-z_][A-Za-z0-9_]*))\s*\}\}"
    r"\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$"
)


def _condition_expression(when: str) -> str:
    """The inLUMEN condition for an Argo ``when``.

    Only one comparison of one task output with a literal can be translated;
    anything else gives "" and the original text stays in the description.
    """
    match = _WHEN_COMPARISON.match(when)
    if not match:
        return ""
    parameter, operator, literal = match.groups()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", literal) and literal not in {"true", "false", "null"}:
        literal = f'"{literal}"'  # a bare word is a string in Argo
    expression = f"value{'.' + parameter if parameter else ''} {operator} {literal}"
    return expression if FLOW_EXPRESSION_PATTERN.fullmatch(expression) else ""


def _template_tasks(template: dict) -> list[dict] | None:
    """The tasks of a ``dag`` or ``steps`` template, each with the tasks it is ordered after."""
    tasks: list[dict] = []
    if isinstance(template.get("dag"), dict):
        for task in template["dag"].get("tasks") or []:
            if isinstance(task, dict) and task.get("name"):
                tasks.append({**task, "_explicit": set(str(item) for item in task.get("dependencies") or [])})
        return tasks
    if isinstance(template.get("steps"), list):
        previous: list[str] = []
        for group in template["steps"]:
            members = [step for step in (group if isinstance(group, list) else [group]) if isinstance(step, dict) and step.get("name")]
            for step in members:
                tasks.append({**step, "_explicit": set(previous)})
            previous = [str(step["name"]) for step in members] or previous
        return tasks
    return None


def _incoming(task: dict, known: list[str]) -> dict[str, tuple[str, str]]:
    """Who feeds a task, by the names of its own scope: named data references first, then plain ordering."""
    task_name = str(task["name"])
    incoming: dict[str, tuple[str, str]] = {}
    for source, argument, output in _references({key: task.get(key) for key in ("arguments", "when", "withParam", "withItems")}):
        if source in known and source != task_name:
            incoming.setdefault(source, (argument, output))
    ordering = set(task["_explicit"])
    if isinstance(task.get("depends"), str):
        expression = _DEPENDS_SUFFIX.sub("", task["depends"])
        ordering |= {token for token in re.findall(r"[A-Za-z0-9_.-]+", expression) if token in known}
    for source in sorted(ordering & set(known), key=known.index):
        if source != task_name:
            incoming.setdefault(source, ("", ""))
    return incoming


def _argo_tasks(spec: dict, notes: list[str]) -> tuple[list[dict], dict[str, dict]]:
    templates = {
        str(item.get("name")): item
        for item in spec.get("templates") or []
        if isinstance(item, dict) and item.get("name")
    }
    entry_name = str(spec.get("entrypoint") or "")
    entry = templates.get(entry_name)
    if entry is None:
        if isinstance(spec.get("workflowTemplateRef"), dict):
            raise ArtifactError(
                "This workflow only refers to a WorkflowTemplate "
                f"({spec['workflowTemplateRef'].get('name')}). Import that template file instead."
            )
        # A WorkflowTemplate may have no entrypoint: use its only DAG or steps template.
        composite = [item for item in templates.values() if "dag" in item or "steps" in item]
        if len(composite) == 1:
            entry = composite[0]
        elif len(templates) == 1:
            entry = next(iter(templates.values()))
        else:
            raise ArtifactError("The workflow names no entrypoint template, and more than one could be meant.")
        notes.append(f"No entrypoint was named; the template “{entry.get('name')}” was used.")

    tasks = _template_tasks(entry)
    if tasks is None:
        tasks = [{"name": entry.get("name") or "step", "template": entry.get("name"), "_explicit": set()}]
        notes.append("The entrypoint is a single template, so the design has one step.")
    return tasks, templates


def _expanded(tasks: list[dict], templates: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Every task with a unique name and its resolved inputs; nested templates opened one level.

    A task that runs a ``dag`` or ``steps`` template is replaced by the tasks
    of that template: its inputs go to the first of them and its consumers
    read from the last. A nested task with a condition or a loop stays one
    step, because the condition or loop applies to the whole of it.
    """
    names = [str(task["name"]) for task in tasks]
    if len(set(names)) != len(names):
        raise ArtifactError("The workflow lists a task name twice.")
    outer = {str(task["name"]): _incoming(task, names) for task in tasks}
    stands_for: dict[str, list[str]] = {}  # an outer name -> the steps its consumers read from
    inner_of: dict[str, tuple[list[dict], dict[str, dict]]] = {}
    expanded: list[str] = []
    for task in tasks:
        task_name = str(task["name"])
        template = templates.get(str(task.get("template") or ""))
        inner = _template_tasks(template) if isinstance(template, dict) else None
        if inner and not task.get("when") and not any(key in task for key in _LOOP_KEYS):
            inner_names = [str(item["name"]) for item in inner]
            if len(set(inner_names)) == len(inner_names):
                links = {str(item["name"]): _incoming(item, inner_names) for item in inner}
                fed = {source for incoming in links.values() for source in incoming}
                stands_for[task_name] = [f"{task_name}/{item}" for item in inner_names if item not in fed]
                inner_of[task_name] = (inner, links)
                expanded.append(task_name)
                continue
        stands_for[task_name] = [task_name]

    def resolved(incoming: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
        return {unique: link for source, link in incoming.items() for unique in stands_for[source]}

    records: list[dict] = []
    for task in tasks:
        task_name = str(task["name"])
        if task_name not in inner_of:
            records.append({"task": task, "name": task_name, "incoming": resolved(outer[task_name]), "parent": ""})
            continue
        inner, links = inner_of[task_name]
        for item in inner:
            own = {f"{task_name}/{source}": link for source, link in links[str(item["name"])].items()}
            # The first tasks of the nested template read what the nested task reads.
            records.append({
                "task": item, "name": f"{task_name}/{item['name']}",
                "incoming": own or resolved(outer[task_name]), "parent": task_name,
            })
    return records, expanded


def _is_inlumen_export(tasks: list[dict], templates: dict[str, dict]) -> bool:
    return any(_annotation(templates.get(str(task.get("template") or "")), "flow-id") for task in tasks)


def _annotation(template: dict | None, key: str) -> str:
    metadata = template.get("metadata") if isinstance(template, dict) else None
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    return str(annotations.get(f"inlumen.ai/{key}") or "").strip() if isinstance(annotations, dict) else ""


def _port_ids(template: dict, side: str, prefix: str) -> dict[str, str]:
    """Argo artifact name -> inLUMEN port id, read from the path the export gives each port."""
    block = template.get(side) if isinstance(template.get(side), dict) else {}
    found: dict[str, str] = {}
    for artifact in block.get("artifacts") or []:
        if isinstance(artifact, dict) and str(artifact.get("path") or "").startswith(prefix):
            port_id = str(artifact["path"])[len(prefix):]
            if port_id and "/" not in port_id:
                found[str(artifact.get("name") or "")] = port_id
    return found


def _flow_behavior(template: dict, inputs: set[str], outputs: set[str], parameters: dict) -> str:
    """Condition, Parallel Map or Collect for an exported Flow step, or "" when the file does not say."""
    named = _annotation(template, "template")
    if named in FLOW_BEHAVIORS:
        return named
    if outputs & {"when_true", "when_false"} or "value" in inputs or "expression" in parameters:
        return "Condition"
    if "item" in outputs or "items" in inputs or {"max_concurrency", "failure_policy"} & set(parameters):
        return "Parallel Map"
    if "items" in outputs or "item" in inputs:
        return "Collect"
    return ""


def _design_from_inlumen_argo(tasks: list[dict], templates: dict[str, dict], notes: list[str]) -> dict:
    """The design of a workflow that inLUMEN exported, read from what the export recorded."""
    steps: dict[str, dict] = {}  # Argo task name -> step
    skipped: list[str] = []
    for task in tasks:
        template = templates.get(str(task.get("template") or "")) or {}
        flow_id = _annotation(template, "flow-id")
        if not flow_id or flow_id in {step["id"] for step in steps.values()}:
            skipped.append(str(task["name"]))
            continue
        container = template.get("container") if isinstance(template.get("container"), dict) else {}
        env = {
            str(item.get("name")): item["value"]
            for item in container.get("env") or []
            if isinstance(item, dict) and isinstance(item.get("value"), str)
        }
        try:
            parameters = json.loads(env.get("INLUMEN_PARAMS_JSON") or "{}")
        except ValueError:
            parameters = {}
        kind = _annotation(template, "type").lower()
        steps[str(task["name"])] = {
            "id": flow_id, "task": task, "template": template,
            "kind": kind if kind in _STEP_KINDS else "task",
            "label": _annotation(template, "label") or _humanize(str(task["name"])),
            "description": env.get("INLUMEN_STEP_DESCRIPTION", ""),
            "parameters": parameters if isinstance(parameters, dict) else {},
            "inputs": _port_ids(template, "inputs", "/inlumen/staging/"),
            "outputs": _port_ids(template, "outputs", "/inlumen/outputs/"),
        }
    for step in steps.values():
        step["behavior"] = (
            _flow_behavior(step["template"], set(step["inputs"].values()), set(step["outputs"].values()), step["parameters"])
            if step["kind"] == "flow" else _annotation(step["template"], "template")
        )

    # Files exported before the behavior was recorded: a Flow step after a
    # Parallel Map that nothing else identifies is the Collect that closes it.
    def after_map(task_name: str, trail: frozenset[str] = frozenset()) -> bool:
        for parent in steps[task_name]["task"].get("dependencies") or []:
            parent = str(parent)
            if parent not in steps or parent in trail:
                continue
            behavior = steps[parent]["behavior"]
            if behavior == "Parallel Map" or (behavior != "Collect" and after_map(parent, trail | {task_name})):
                return True
        return False

    for task_name, step in steps.items():
        if step["kind"] == "flow" and not step["behavior"] and after_map(task_name):
            step["behavior"] = "Collect"

    def default_port(step: dict, side: str) -> str:
        ports = ports_for_template(None, step["kind"], step["behavior"])[side]
        return str(ports[0]["id"]) if ports else ("input" if side == "inputs" else "output")

    edges: list[dict] = []
    guessed_branches: list[str] = []
    branch_count: dict[str, int] = defaultdict(int)
    extra_inputs: dict[str, list[str]] = defaultdict(list)
    for task_name, step in steps.items():
        task = step["task"]
        bound: set[str] = set()
        arguments = task.get("arguments") if isinstance(task.get("arguments"), dict) else {}
        for artifact in arguments.get("artifacts") or []:
            match = _TASK_REFERENCE.search(str(artifact.get("from") or "")) if isinstance(artifact, dict) else None
            if not match or match.group(1) not in steps or match.group(1) == task_name:
                continue
            source = steps[match.group(1)]
            name = str(artifact.get("name") or "")
            edges.append(_edge(
                source["id"], source["outputs"].get(match.group(3) or "", (match.group(3) or "output").replace("-", "_")),
                step["id"], step["inputs"].get(name, name.replace("-", "_")),
            ))
            bound.add(match.group(1))
        # An export without generated code records the order only, not the ports.
        unbound = [str(item) for item in task.get("dependencies") or [] if str(item) in steps and str(item) not in bound]
        for source_name in unbound:
            source = steps[source_name]
            source_port = default_port(source, "outputs")
            if source["behavior"] == "Condition":
                source_port = "when_true" if branch_count[source_name] == 0 else "when_false"
                branch_count[source_name] += 1
                if source["label"] not in guessed_branches:
                    guessed_branches.append(source["label"])
            target_port = default_port(step, "inputs")
            if len(unbound) + len(bound) > 1 and step["kind"] != "flow":
                target_port = _slug(source["label"].lower(), "input")
                extra_inputs[task_name].append(target_port)
            edges.append(_edge(source["id"], source_port, step["id"], target_port))

    nodes, unknown_flows = [], []
    for task_name, step in steps.items():
        kind = step["kind"]
        if kind == "flow":
            ports = ports_for_template(None, kind, step["behavior"])
            if not step["behavior"]:
                unknown_flows.append(step["label"])
        else:
            def listed(ids: list[str]) -> list[dict[str, Any]]:
                return [{"id": item, "name": item, "type": "any", "required": True, "description": ""} for item in ids]

            declared: dict[str, Any] = {}
            inputs = list(dict.fromkeys([*step["inputs"].values(), *extra_inputs[task_name]]))
            if inputs:
                declared["inputs"] = listed(inputs)
            if step["outputs"]:
                declared["outputs"] = listed(list(step["outputs"].values()))
            ports = normalize_node_ports(declared, kind)
        data: dict[str, Any] = {"type": kind, "label": step["label"], "description": step["description"], "ports": ports}
        if step["behavior"]:
            data["template_label"] = step["behavior"]
        if step["parameters"]:
            data["param"] = step["parameters"]
        nodes.append({"id": step["id"], "type": "custom", "position": {"x": 0.0, "y": 0.0}, "data": data})

    notes.insert(0, "This workflow was exported by inLUMEN. The steps, their types, labels, descriptions and "
                    "parameters, and the connections were read back from it.")
    notes.append("Port types, sensitive parameters, code and data are not in a workflow file. "
                 "Import the bundle ZIP to get the code and data as well.")
    if guessed_branches:
        notes.append("The file does not say which branch of a Condition a step follows. The first step after it was "
                     "connected to “when true” and the second to “when false”: " + ", ".join(guessed_branches) + ".")
    if unknown_flows:
        notes.append("The file does not say whether these Flow steps are a Condition, a Parallel Map or a Collect: "
                     + ", ".join(unknown_flows) + ".")
    if skipped:
        notes.append("Tasks that are not steps of the design were left out: " + ", ".join(skipped) + ".")
    return {"nodes": nodes, "edges": edges}


def _design_from_foreign_argo(tasks: list[dict], templates: dict[str, dict], notes: list[str]) -> dict:
    """The design of a workflow of any origin, from its tasks, dependencies, conditions and loops."""
    records, expanded = _expanded(tasks, templates)
    if len(records) > MAX_STEPS:
        raise ArtifactError(f"The workflow has more than {MAX_STEPS} tasks.")
    names = [record["name"] for record in records]
    links = {record["name"]: record["incoming"] for record in records}
    children: dict[str, set[str]] = defaultdict(set)
    for task_name, incoming in links.items():
        for source in incoming:
            children[source].add(task_name)

    def referenced(record: dict, key: str) -> list[str]:
        """The predecessors that a field of the task names, in this task's resolved inputs."""
        prefix = f"{record['parent']}/" if record["parent"] else ""
        found = []
        for source, _argument, _output in _references(record["task"].get(key)):
            for unique in links[record["name"]]:
                if unique == prefix + source or unique.startswith(f"{source}/"):
                    found.append(unique)
        return found

    # First pass: which steps each task becomes, and where its consumers read from.
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return str(counter)

    plans: dict[str, dict] = {}
    conditions_kept, untranslated, nested, by_reference = [], [], [], []
    for record in records:
        task, task_name, incoming = record["task"], record["name"], links[record["name"]]
        when = str(task.get("when") or "").strip()
        looped = any(key in task for key in _LOOP_KEYS)
        map_source = None
        if looped and incoming:
            map_source = (referenced(record, "withParam") or [s for s, (a, _o) in incoming.items() if not a] or list(incoming))[0]
        condition_source = None
        if when:
            candidates = referenced(record, "when") or (list(incoming) if len(incoming) == 1 else [])
            # Inside a loop the condition is checked for each item, which a Condition before the loop cannot do.
            if candidates and "{{item" not in when and (not looped or candidates[0] == map_source):
                condition_source = candidates[0]
            else:
                conditions_kept.append(task_name)
        kind = "task"
        if len(names) > 1 and not looped:
            if not incoming and children[task_name]:
                kind = "source"
            elif incoming and not children[task_name]:
                kind = "destination"
        plan = {
            "kind": kind, "when": when, "looped": looped,
            "condition_source": condition_source, "map_source": map_source,
            "items": next_id() if looped and not incoming else "",
            "condition": next_id() if condition_source else "",
            "map": next_id() if looped else "",
            "id": next_id(),
            "collect": next_id() if looped else "",
        }
        plan["exit"] = (plan["collect"], "items") if looped else (plan["id"], {"source": "data"}.get(kind, "output"))
        plans[task_name] = plan
        template = templates.get(str(task.get("template") or ""))
        if isinstance(template, dict) and ("dag" in template or "steps" in template):
            nested.append(task_name)
        if task.get("templateRef"):
            by_reference.append(task_name)

    def node(node_id: str, kind: str, label: str, description: str, ports: dict, behavior: str = "", param: dict | None = None) -> dict:
        data: dict[str, Any] = {"type": kind, "label": label, "description": description, "ports": ports}
        if behavior:
            data["template_label"] = behavior
        if param:
            data["param"] = param
        return {"id": node_id, "type": "custom", "position": {"x": 0.0, "y": 0.0}, "data": data}

    def flow(node_id: str, behavior: str, label: str, description: str, param: dict | None = None) -> dict:
        return node(node_id, "flow", label, description, ports_for_template(None, "flow", behavior), behavior, param)

    # Second pass: the steps and their connections.
    nodes, edges = [], []
    for record in records:
        task, task_name, incoming = record["task"], record["name"], links[record["name"]]
        plan = plans[task_name]
        kind, label = plan["kind"], _humanize(str(task["name"]))
        quoted = f"Argo task “{task['name']}”" + (f" of the nested task “{record['parent']}”" if record["parent"] else "")
        loop_text = next((f"{key}: {json.dumps(task[key], ensure_ascii=False)[:200]}" for key in _LOOP_KEYS if key in task), "")

        if plan["items"]:
            nodes.append(node(plan["items"], "source", f"{label} items",
                              f"The list that {quoted} runs over ({loop_text}).", normalize_node_ports(None, "source")))
            edges.append(_edge(plan["items"], "data", plan["map"], "items"))
        if plan["condition"]:
            expression = _condition_expression(plan["when"])
            if not expression:
                untranslated.append(task_name)
            nodes.append(flow(
                plan["condition"], "Condition", f"{label}?",
                f"Condition of {quoted}: {plan['when'][:300]}"
                + ("" if expression else " This expression could not be translated; enter it for this step."),
                {"expression": expression} if expression else None,
            ))
        if plan["map"]:
            nodes.append(flow(plan["map"], "Parallel Map", f"For each item of {label}",
                              f"Runs {quoted} once for each item ({loop_text})."))

        ports: list[dict[str, Any]] = []
        used: set[str] = set()

        def input_port(source: str, argument: str) -> str:
            if len(incoming) <= 1 and not argument:
                port_id = {"task": "input", "destination": "data"}.get(kind, "input")
            else:
                port_id = _slug(argument or source.split("/")[-1], "input")
            while port_id in used:
                port_id += "_2"
            used.add(port_id)
            ports.append({"id": port_id, "name": argument or _humanize(source.split("/")[-1]), "type": "any", "required": True, "description": ""})
            return port_id

        if plan["items"]:
            edges.append(_edge(plan["map"], "item", plan["id"], input_port("item", "")))
        for source, (argument, _output) in incoming.items():
            start = plans[source]["exit"]
            port_id = input_port(source, argument)
            if source == plan["condition_source"]:
                edges.append(_edge(*start, plan["condition"], "value"))
                start = (plan["condition"], "when_true")
            if source == plan["map_source"]:
                edges.append(_edge(*start, plan["map"], "items"))
                start = (plan["map"], "item")
            edges.append(_edge(*start, plan["id"], port_id))

        template = templates.get(str(task.get("template") or ""))
        sentences = [quoted + "."]
        if task_name in nested:
            sentences.append(f"Runs the nested template “{task.get('template')}”, which has its own tasks.")
        elif task_name in by_reference:
            reference = task["templateRef"] if isinstance(task["templateRef"], dict) else {}
            sentences.append(f"Runs the template “{reference.get('template')}” of “{reference.get('name')}”.")
        elif _what_runs(template):
            sentences.append(_what_runs(template))
        if task_name in conditions_kept:
            sentences.append(f"Runs only when: {plan['when'][:200]}")
        declared: dict[str, Any] = {"inputs": ports} if kind != "source" and ports else {}
        nodes.append(node(plan["id"], kind, label, " ".join(sentences), normalize_node_ports(declared, kind)))
        if plan["collect"]:
            nodes.append(flow(plan["collect"], "Collect", f"Collect {label} results",
                              f"Gathers the results of {quoted} for all items."))
            edges.append(_edge(plan["id"], "output", plan["collect"], "item"))

    notes.insert(0, "The design was built from the workflow's tasks and their dependencies. "
                    "A task without a predecessor became a Source, one without a successor a Destination.")
    notes.append("Port types, parameters, code and data are not read from the workflow; every port has the type “any”.")
    with_condition = [name for name, plan in plans.items() if plan["condition"]]
    if with_condition:
        notes.append("A Condition step was placed before each task with a condition (when): " + ", ".join(with_condition)
                     + ". Argo skips such a task when the condition is false, so the “when false” output is not connected.")
    if untranslated:
        notes.append("These conditions are not one comparison of a task output with a value, so the original text is "
                     "in the description of the Condition and its expression is empty: " + ", ".join(untranslated) + ".")
    if conditions_kept:
        notes.append("These conditions do not test the output of one earlier task, or are checked for each item of a "
                     "loop, and were kept as text in the step description: " + ", ".join(conditions_kept) + ".")
    looped_names = [name for name, plan in plans.items() if plan["looped"]]
    if looped_names:
        notes.append("Each loop (withItems, withParam, withSequence) became Parallel Map, the task and Collect: "
                     + ", ".join(looped_names) + ". The concurrency and the failure policy are not set.")
    if expanded:
        notes.append("Nested workflows were opened into their own tasks: " + ", ".join(expanded) + ".")
    if nested:
        notes.append("These nested workflows were kept as one step each (a nested workflow with a condition or a loop, "
                     "or inside another nested workflow): " + ", ".join(nested) + ".")
    if by_reference:
        notes.append("Tasks that use a template from another file were kept as one step each: " + ", ".join(by_reference) + ".")
    return {"nodes": nodes, "edges": edges}


def design_from_argo_workflow(document: Any, *, name: str = "") -> dict[str, Any]:
    """Steps and connections of one Argo Workflows document (already parsed).

    A workflow that inLUMEN exported records the type, label, description,
    parameters and ports of each step, and these are read back. For a
    workflow of any other origin the tasks become steps, a ``when`` becomes a
    Condition, a loop becomes Parallel Map and Collect, and a nested template
    is opened one level.
    """
    if not isinstance(document, dict) or document.get("kind") not in ARGO_KINDS:
        raise ArtifactError("This is not an Argo Workflow, WorkflowTemplate or CronWorkflow.")
    spec = document.get("spec") if isinstance(document.get("spec"), dict) else {}
    if document["kind"] == "CronWorkflow":
        spec = spec.get("workflowSpec") if isinstance(spec.get("workflowSpec"), dict) else {}
    notes: list[str] = []
    tasks, templates = _argo_tasks(spec, notes)
    if not tasks:
        raise ArtifactError("The workflow's entrypoint has no tasks.")
    if len(tasks) > MAX_STEPS:
        raise ArtifactError(f"The workflow has more than {MAX_STEPS} tasks.")
    if _is_inlumen_export(tasks, templates):
        graph = _design_from_inlumen_argo(tasks, templates, notes)
    else:
        graph = _design_from_foreign_argo(tasks, templates, notes)
    if len(graph["nodes"]) > MAX_STEPS:
        raise ArtifactError(f"The design would have more than {MAX_STEPS} steps.")
    _relayout(graph)
    if spec.get("onExit"):
        notes.append(f"The exit handler “{spec['onExit']}” is not part of the design.")
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    workflow_name = str(metadata.get("name") or metadata.get("generateName") or "").rstrip("-")
    description = f"Imported from the Argo {document['kind']}" + (f" “{workflow_name}”." if workflow_name else ".")
    return _result("argo-workflow", graph, name or _humanize(workflow_name) or "Imported workflow", description, notes)


def _argo_document(text: str) -> dict:
    import yaml

    try:
        documents = [item for item in yaml.safe_load_all(text) if isinstance(item, dict)]
    except yaml.YAMLError:
        raise ArtifactError("The file is not valid YAML or JSON.") from None
    workflows = [item for item in documents if item.get("kind") in ARGO_KINDS]
    if not workflows:
        raise ArtifactError(
            "The file holds no Argo Workflow, WorkflowTemplate or CronWorkflow. "
            "Supported files: an inLUMEN bundle ZIP, its bundle-manifest.json, or an Argo Workflows file."
        )
    return workflows[0] if len(workflows) == 1 else {**workflows[0], "_others": len(workflows) - 1}


# ------------------------------------------------------------------ entry

def design_from_artifact(data: bytes, *, filename: str = "") -> dict[str, Any]:
    """Recognize the artifact in ``data`` and return its design.

    Returns ``{"kind", "project", "notes", "step_count", "connection_count"}``.
    For a ZIP of code it returns ``{"kind": "code-zip", "needs_model": True,
    "files_to_send", "files_not_sent", ...}`` and builds nothing: see
    :func:`design_from_code_zip`. Raises :class:`ArtifactError` with a message
    for the user otherwise.
    """
    if not data:
        raise ArtifactError("The file is empty.")
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ArtifactError("The file is larger than 50 MiB.")
    stem = re.sub(r"\.(zip|ya?ml|json)$", "", filename.replace("\\", "/").split("/")[-1], flags=re.IGNORECASE)
    if data[:4] == b"PK\x03\x04":
        with _open_zip(data) as archive:
            manifest = _manifest_from_zip(archive)
            if manifest is not None:
                return design_from_bundle_manifest(
                    manifest, name=_humanize(stem), run_spec=_run_spec_from_zip(archive)
                )
            argo = _argo_in_zip(archive)
            if argo is not None:
                path, document, others = argo
                result = design_from_argo_workflow(document)
                result["notes"].append(f"The workflow file {path} of the ZIP was imported.")
                if others:
                    result["notes"].append(f"The ZIP holds {others} more workflow file(s); only this one was imported.")
                return result
            code = code_files_from_zip(archive)
        if not code["has_code"]:
            raise ArtifactError(
                f"The ZIP holds no {BUNDLE_MANIFEST}, no Argo Workflows file and no code file, "
                "so no design can be built from it."
            )
        # A design can only be inferred from code by the model. The caller asks
        # the user first, then calls design_from_code_zip.
        return {
            "kind": "code-zip",
            "needs_model": True,
            "name": _humanize(stem),
            "files_to_send": [{key: item[key] for key in ("path", "part", "chars")} for item in code["send"]],
            "files_not_sent": code["listed"],
            "total_chars": code["total_chars"],
        }
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ArtifactError("The file is too large to be a workflow file or a bundle manifest.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ArtifactError("The file is neither a ZIP nor a text file.") from None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and str(parsed.get("schema_version") or "").startswith(BUNDLE_SCHEMA_PREFIX):
        return design_from_bundle_manifest(parsed, name=_humanize(stem))
    document = _argo_document(text)
    others = document.pop("_others", 0)
    result = design_from_argo_workflow(document)
    if others:
        result["notes"].append(f"The file holds {others} more workflow document(s); only the first was imported.")
    return result


async def design_from_code_zip(data: bytes, *, model: Any, filename: str = "") -> dict[str, Any]:
    """Let the model infer the design of a ZIP of code, in one call.

    Sends the files that :func:`code_files_from_zip` selects to ``model``.
    Call it only after the user agreed to that.
    """
    from inlumen.design.from_code import CodeFile, design_from_code

    if len(data) > MAX_ARTIFACT_BYTES:
        raise ArtifactError("The file is larger than 50 MiB.")
    with _open_zip(data) as archive:
        code = code_files_from_zip(archive)
        sizes = {_member_path(info): info.file_size for info in archive.infolist() if not info.is_dir()}
    if not code["has_code"]:
        raise ArtifactError("The ZIP holds no code file.")
    stem = re.sub(r"\.zip$", "", filename.replace("\\", "/").split("/")[-1], flags=re.IGNORECASE)
    try:
        design = await design_from_code(
            [CodeFile(item["path"], item["text"], item["part"]) for item in code["send"]],
            model=model,
            other_files=[item["path"] for item in code["listed"]],
            name=_humanize(stem),
        )
    except ValueError as exc:
        raise ArtifactError(f"The model built no design from this code: {exc}") from None
    # The steps are placed one at a time as they are created, which can put branches on
    # top of each other. The finished design is laid out as a whole.
    from inlumen.contracts.project_ir import project_document as _project_document

    _relayout(design.graph)
    design.project = _project_document(
        design.graph, name=design.project["project"]["name"], description=design.project["project"]["description"]
    )
    notes = [
        "The design was inferred from the code by the model. It is the model's reading of the code, "
        "not a guaranteed description: check each step against the code.",
    ]
    summary = re.sub(r"\s+", " ", re.sub(r"[*`#]+", "", design.message or "")).strip()
    if summary:
        notes.append(summary)
    notes.append(
        f"{len(code['send'])} file(s) were read"
        + (f"; {len(code['listed'])} other file(s) were not read." if code["listed"] else ".")
        + " No code was attached to the Tasks: the code of the ZIP is not in inLUMEN's Task format."
    )
    # The input file that the model named for a Source is attached by the canvas.
    # The path is the model's word, so only a visible file of this ZIP counts.
    labels = {str(node["id"]): str(node["data"].get("label") or node["id"]) for node in design.graph["nodes"]}
    kinds = {str(node["id"]): str(node["data"].get("type") or "") for node in design.graph["nodes"]}
    def is_hidden(path: str) -> bool:
        return any(part.startswith(".") or part.lower() in SKIPPED_FOLDERS for part in path.split("/"))

    attachable = [path for path, size in sizes.items() if not is_hidden(path) and 0 < size <= MAX_SOURCE_FILE_BYTES]

    def in_zip(named: str) -> str:
        """The file of the ZIP that a named path means: itself, or the only file whose path ends with it."""
        named = re.sub(r"^(\./)+", "", named.replace("\\", "/")).strip("/")
        if not named or ".." in named.split("/"):
            return ""
        if named in attachable:
            return named
        # The model often leaves out the top folder of the ZIP.
        endings = [path for path in attachable if path.endswith("/" + named)]
        return endings[0] if len(endings) == 1 else ""

    source_files, not_found = [], []
    for node_id, named in design.source_files.items():
        if kinds.get(node_id) != "source":
            continue
        path = in_zip(named)
        if path:
            source_files.append({"node_id": node_id, "label": labels[node_id], "path": path})
        else:
            not_found.append(f"{labels[node_id]} ({named})")
    if not_found:
        notes.append("The input file named for these Sources is not a file of the ZIP that can be attached: "
                     + "; ".join(not_found) + ".")
    notes += [f"The design does not pass validation yet: {item}" for item in design.validation_errors]
    return {
        "kind": "code-zip",
        "project": design.project,
        "notes": notes,
        "step_count": len(design.graph["nodes"]),
        "connection_count": len(design.graph["edges"]),
        "model_calls": len(design.requests),
        "source_files": source_files,
    }
