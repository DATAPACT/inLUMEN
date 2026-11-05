"""Build the code generator's input from a canvas graph.

The canvas graph is what the editor, ``inlumen.design.run.design()`` and the
Project JSON import produce. Where attached files live is the caller's
business: pass ``bucket_for`` to name a step's storage bucket and ``sample`` to
describe a file's contents (the inLUMEN backend reads MinIO; a library caller
can use :func:`directory_sampler`).
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

from inlumen.contracts.artifact_contract import classify_artifact
from inlumen.contracts.node_definitions.artifacts import implementation_plan_from_data
from inlumen.contracts.node_ports import normalize_node_ports
from inlumen.contracts.step_types import normalize_step_type
from inlumen.contracts.task_package_contract import TASK_METADATA_FILENAME

PIPELINE_CONTEXT_SCHEMA_VERSION = "inlumen.pipeline-script-generation-context@1"

DEFAULT_ALLOWED_PACKAGES = [
    "pandas",
    "numpy",
    "pillow",
    "scikit-learn",
    "requests",
    "pypdf",
    "SpeechRecognition",
    "pocketsphinx",
    "textblob",
]
CODEGEN_RUNTIME_FILENAMES = {
    "main.py",
    "requirements.txt",
    TASK_METADATA_FILENAME,
    "node-manifest.json",
    "validation-report.json",
}
DEFAULT_SAMPLE_BINARY_MAX_BYTES = 16 * 1024 * 1024
SAMPLE_TEXT_MAX_CHARS = 12000

# (bucket, filename, entry) -> fields to merge into the file entry.
Sampler = Callable[[str, str, dict[str, Any]], dict[str, Any]]
BucketNamer = Callable[[str], str]


def local_bucket_name(flow_id: str) -> str:
    """The bucket a step's files use in a local (single-workspace) inLUMEN."""
    return f"files-step-id-{str(flow_id or '').strip().lower()}"


def node_data(node: dict[str, Any]) -> dict[str, Any]:
    data = node.get("data")
    return data if isinstance(data, dict) else node


def is_codegen_runtime_file(filename: str) -> bool:
    """Whether a file is generated code rather than data the step reads."""
    lower = str(filename or "").strip().lower()
    return (
        lower in CODEGEN_RUNTIME_FILENAMES
        or lower.startswith("dockerfile.")
        or lower.endswith((".py", ".pyi", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".tsx"))
    )


def sample_from_bytes(
    content: bytes,
    descriptor: dict[str, Any],
    *,
    binary_max_bytes: int = DEFAULT_SAMPLE_BINARY_MAX_BYTES,
) -> dict[str, Any]:
    """Describe a file's contents for the model and the validation sandbox.

    Tables and JSON get columns and up to ten rows, text its first 12000
    characters; binary files travel whole (base64) up to ``binary_max_bytes``.
    """
    kind = descriptor.get("kind")
    file_format = descriptor.get("format")
    if kind not in {"table", "json", "text"}:
        if len(content) > binary_max_bytes:
            return {
                "size_bytes": len(content),
                "sample_transport_error": (
                    f"Real input exceeds the {binary_max_bytes}-byte "
                    "binary validation transport limit."
                ),
            }
        return {
            "size_bytes": len(content),
            "sample": {
                "content_base64": base64.b64encode(content).decode("ascii"),
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "omitted_bytes": 0,
            },
        }
    text = content[:SAMPLE_TEXT_MAX_CHARS].decode("utf-8", errors="replace")
    omitted = max(0, len(content) - len(content[:SAMPLE_TEXT_MAX_CHARS]))
    if kind == "table" and file_format in {"csv", "tsv"}:
        delimiter = "\t" if file_format == "tsv" else ","
        try:
            reader = csv.DictReader(StringIO(text), delimiter=delimiter)
            rows = [row for _, row in zip(range(10), reader)]
            columns = list(reader.fieldnames or [])
        except Exception:
            return {"sample": {"text": text, "omitted_bytes": omitted}}
        return {
            "columns": columns,
            "sample": {
                "rows": rows,
                "omitted_bytes": omitted,
            },
            "size_bytes": len(content),
        }
    if kind == "json":
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        rows = []
        columns = []
        if isinstance(parsed, list):
            rows = [item for item in parsed[:10] if isinstance(item, dict)]
        elif isinstance(parsed, dict):
            candidate_rows = parsed.get("records") or parsed.get("items")
            if isinstance(candidate_rows, list):
                rows = [item for item in candidate_rows[:10] if isinstance(item, dict)]
        if rows:
            columns = sorted({key for row in rows for key in row})
        return {
            **({"columns": columns} if columns else {}),
            "sample": {
                **({"rows": rows} if rows else {"text": text}),
                "omitted_bytes": omitted,
            },
            "size_bytes": len(content),
        }
    return {
        "sample": {"text": text, "omitted_bytes": omitted},
        "size_bytes": len(content),
    }


def directory_sampler(
    root: str | Path,
    *,
    binary_max_bytes: int = DEFAULT_SAMPLE_BINARY_MAX_BYTES,
) -> Sampler:
    """Sample attached files from a local folder.

    A file attached to a step is looked up as ``root/<flow_id>/<filename>``
    (bucket ``files-step-id-<flow_id>``), then as ``root/<filename>``. Missing
    files are described by name only, as when storage is unavailable.
    """
    base = Path(root)
    # Bucket names are lowercase; step folders keep the step id's case.
    folders = {path.name.lower(): path for path in base.iterdir() if path.is_dir()} if base.is_dir() else {}

    def sample(bucket: str, filename: str, entry: dict[str, Any]) -> dict[str, Any]:
        folder = folders.get(bucket.removeprefix("files-step-id-"))
        candidates = ([folder / filename] if folder is not None else []) + [base / filename]
        for candidate in candidates:
            if candidate.is_file():
                return sample_from_bytes(candidate.read_bytes(), entry, binary_max_bytes=binary_max_bytes)
        return {}

    return sample


def node_file_entries(
    node: dict[str, Any],
    *,
    bucket_for: BucketNamer = local_bucket_name,
    sample: Sampler | None = None,
    include_runtime_artifacts: bool = False,
) -> list[dict[str, Any]]:
    """The data files attached to a step, sampled when ``sample`` is given."""
    data = node_data(node)
    raw_files = (
        data.get("file_buckets")
        if isinstance(data.get("file_buckets"), list)
        else data.get("files")
    )
    if not isinstance(raw_files, list):
        return []
    flow_id = str(node.get("id") or data.get("flow_id") or data.get("id") or "").strip()
    default_bucket = bucket_for(flow_id)
    entries = []
    for item in raw_files:
        if isinstance(item, str):
            filename = item.strip()
            bucket = default_bucket
            content_type = None
        elif isinstance(item, dict):
            filename = str(item.get("filename") or item.get("name") or "").strip()
            bucket = str(item.get("bucket") or default_bucket).strip().lower()
            content_type = item.get("content_type")
        else:
            continue
        if not filename:
            continue
        if not include_runtime_artifacts and is_codegen_runtime_file(filename):
            continue
        entry = {
            "filename": filename,
            "bucket": bucket,
            "content_type": content_type,
            "role": item.get("role") if isinstance(item, dict) else None,
            **classify_artifact(filename),
        }
        if isinstance(item, dict) and item.get("snapshot_bucket") and item.get("snapshot_object"):
            entry["snapshot_bucket"] = item["snapshot_bucket"]
            entry["snapshot_object"] = item["snapshot_object"]
        if sample is not None:
            entry.update(sample(
                entry.get("snapshot_bucket", bucket), entry.get("snapshot_object", filename), entry
            ))
        entries.append(entry)
    return entries


def node_descriptor(
    node: dict[str, Any],
    *,
    bucket_for: BucketNamer = local_bucket_name,
    sample: Sampler | None = None,
) -> dict[str, Any]:
    """One canvas step as the generator's ``NodeDescriptor`` (secrets left out)."""
    data = node_data(node)
    parameters = (
        dict(data.get("param"))
        if isinstance(data.get("param"), dict)
        else {}
    )
    secret_parameter_names = {
        str(name)
        for name in (data.get("secret_params") or [])
        if str(name).strip()
    }
    parameters = {
        key: value
        for key, value in parameters.items()
        if str(key) not in secret_parameter_names
    }
    content = data.get("content")
    if isinstance(content, str) and content.strip():
        parameters["content"] = content.strip()
    implementation = implementation_plan_from_data(data)
    subpipeline = data.get("subpipeline")
    if not isinstance(subpipeline, dict):
        try:
            subpipeline = json.loads(data.get("subpipeline_json") or "{}")
        except (TypeError, ValueError):
            subpipeline = {}
    return {
        "flow_id": str(node.get("id") or data.get("flow_id") or data.get("id") or ""),
        "label": str(data.get("label") or ""),
        "description": str(data.get("description") or ""),
        "type": normalize_step_type(data.get("type")),
        "template": str(data.get("template_label") or data.get("definition_id") or ""),
        "parameters": parameters,
        "secret_parameters": sorted(secret_parameter_names),
        "implementation": implementation,
        "subpipeline": subpipeline if isinstance(subpipeline, dict) else {},
        "ports": normalize_node_ports(data.get("ports"), data.get("type")),
        "files": node_file_entries(node, bucket_for=bucket_for, sample=sample),
    }


def default_runtime_constraints() -> dict[str, Any]:
    """The runtime inLUMEN generates pipelines for."""
    return {
        "language": "python",
        "python_version": "3.11",
        "base_image": "python:3.11-slim",
        "allowed_packages": list(DEFAULT_ALLOWED_PACKAGES),
        "allow_unlisted_model_packages": True,
        "network_allowed": True,
        "max_runtime_seconds": 900,
    }


def pipeline_context(
    graph: dict[str, Any],
    *,
    bucket_for: BucketNamer = local_bucket_name,
    sample: Sampler | None = None,
) -> dict[str, Any]:
    """A canvas graph as a ``PipelineGenerationContext`` payload."""
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    descriptors = [
        node_descriptor(node, bucket_for=bucket_for, sample=sample)
        for node in nodes
        if isinstance(node, dict)
    ]
    return {
        "schema_version": PIPELINE_CONTEXT_SCHEMA_VERSION,
        "pipeline": graph.get("pipeline") if isinstance(graph.get("pipeline"), dict) else {},
        "graph": {
            "nodes": descriptors,
            "edges": [
                {
                    "source": str(edge.get("source")),
                    "target": str(edge.get("target")),
                    "source_port": str(
                        edge.get("sourceHandle") or edge.get("source_port") or ""
                    ),
                    "target_port": str(
                        edge.get("targetHandle") or edge.get("target_port") or ""
                    ),
                }
                for edge in edges
                if isinstance(edge, dict) and edge.get("source") and edge.get("target")
            ],
        },
        "runtime_constraints": default_runtime_constraints(),
    }
