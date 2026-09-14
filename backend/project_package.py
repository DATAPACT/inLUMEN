"""Portable design archives. No archive entry is ever extracted or executed."""
import copy
import hashlib
import io
import json
import stat
import zipfile

from graph_document import validate_graph_document
from node_parameters import normalize_secret_param_keys, is_sensitive_parameter_name

FORMAT = "inlumen.project-package@1"
MAX_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_MANIFEST = 5 * 1024 * 1024
DATA_KEYS = {
    "label", "description", "type", "template_label", "ports", "param",
    "secret_params", "definition_id", "definition_version", "implementation",
    "template", "endpoint", "database",
}


def clean_graph(graph):
    validate_graph_document(graph)
    result = {"nodes": [], "edges": [], "settings": copy.deepcopy(graph.get("settings", {}))}
    for node in graph["nodes"]:
        raw = node.get("data", {})
        data = {key: copy.deepcopy(value) for key, value in raw.items() if key in DATA_KEYS}
        params = data.get("param", {})
        if not isinstance(params, dict):
            raise ValueError("Runtime parameters must be an object")
        secrets = sorted(set(normalize_secret_param_keys(data.get("secret_params"), params)) |
                         {key for key in params if is_sensitive_parameter_name(key)})
        data["param"] = {key: "" if key in secrets else value for key, value in params.items()}
        data["secret_params"] = secrets
        result["nodes"].append({"id": str(node["id"]), "type": "custom",
                                "position": copy.deepcopy(node.get("position", {"x": 0, "y": 0})), "data": data})
    for edge in graph["edges"]:
        result["edges"].append({key: copy.deepcopy(value) for key, value in edge.items()
                                if key in {"id", "source", "target", "sourceHandle", "targetHandle"}})
    return result


def build_package(graph, read_file):
    """read_file(node_id, reference, remaining_bytes) reads an authorized attachment."""
    manifest = {"format": FORMAT, "name": str(graph.get("pipeline", {}).get("name") or "Pipeline"),
                "description": str(graph.get("pipeline", {}).get("description") or ""), "definitions": {}}
    blobs = {}
    remaining = MAX_BYTES - MAX_MANIFEST

    def pack(source, nested=False):
        nonlocal remaining
        clean = clean_graph(source)
        for original, node in zip(source["nodes"], clean["nodes"]):
            raw = original.get("data", {})
            files = raw.get("file_buckets", raw.get("files", []))
            node["data"]["files"] = []
            names = set()
            for ref in files:
                ref = {"filename": ref} if isinstance(ref, str) else ref
                filename = ref.get("filename") or ref.get("name")
                _filename(filename)
                if filename in names:
                    raise ValueError("Duplicate attachment filename")
                names.add(filename)
                payload = read_file(str(original["id"]), ref, remaining)
                remaining -= len(payload)
                if remaining < 0:
                    raise ValueError("Package attachments exceed 45 MB")
                digest = hashlib.sha256(payload).hexdigest()
                path = f"files/{digest}"
                blobs[path] = payload
                role = ref.get("role") if ref.get("role") in {"code", "data"} else ""
                node["data"]["files"].append({"filename": filename, "role": role,
                                               "path": path, "sha256": digest})
            if raw.get("type") == "subpipeline":
                if nested:
                    raise ValueError("Nested reusable pipelines are not supported")
                sub = raw.get("subpipeline", {})
                reference = sub.get("reference", {})
                uid = str(reference.get("pipeline_uid") or "")
                resolved = sub.get("resolved_graph")
                if not uid or not isinstance(resolved, dict) or sub.get("resolution_error"):
                    raise ValueError("Resolve every reusable pipeline before exporting")
                # A legacy version is a distinct immutable definition.
                key = uid + ":" + str(reference.get("version_uid") or "")
                if key not in manifest["definitions"]:
                    manifest["definitions"][key] = {"name": str(reference.get("pipeline_name") or "Reusable pipeline"),
                                                     "description": str(sub.get("description") or ""),
                                                     "graph": pack(resolved, True)}
                node["data"]["package_definition"] = key
        return clean

    manifest["graph"] = pack(graph)
    encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_MANIFEST or len(blobs) + 1 > MAX_ENTRIES:
        raise ValueError("Package exceeds the supported size")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", encoded)
        for path, payload in blobs.items():
            archive.writestr(path, payload)
    payload = stream.getvalue()
    if len(payload) > MAX_BYTES:
        raise ValueError("Package exceeds 50 MB")
    return payload


def _filename(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 255 or value in {".", ".."} or any(c in value for c in "/\\\x00"):
        raise ValueError("Invalid attachment filename")


def parse_package(payload):
    if len(payload) > MAX_BYTES:
        raise ValueError("Package exceeds 50 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (len(entries) > MAX_ENTRIES or len(set(names)) != len(names) or
                    sum(entry.file_size for entry in entries) > MAX_BYTES):
                raise ValueError("Package contains duplicate entries or exceeds the supported size")
            for entry in entries:
                name = entry.filename
                if (entry.is_dir() or entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16) or
                        (name != "manifest.json" and not (name.startswith("files/") and len(name) == 70 and
                         all(c in "0123456789abcdef" for c in name[6:])))):
                    raise ValueError("Unexpected or unsafe package entry")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST:
                raise ValueError("Package manifest is too large")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise ValueError("Select an inLUMEN project package")
            definitions = manifest.get("definitions")
            if not isinstance(definitions, dict) or len(definitions) > 200:
                raise ValueError("Invalid reusable definitions")
            blobs = {}
            used_definitions = set()
            file_count = secret_count = 0
            graphs = [manifest["graph"]] + [definition["graph"] for definition in definitions.values()]
            for index, graph in enumerate(graphs):
                clean = clean_graph(graph)
                for original, node in zip(graph["nodes"], clean["nodes"]):
                    raw = original.get("data", {})
                    data = node["data"]
                    secret_count += len(data["secret_params"])
                    if data.get("type") == "subpipeline":
                        key = raw.get("package_definition")
                        if index or not isinstance(key, str) or key not in definitions:
                            raise ValueError("Missing or nested reusable definition")
                        used_definitions.add(key)
                        data["package_definition"] = key
                    files = raw.get("files", [])
                    if not isinstance(files, list):
                        raise ValueError("Invalid attachment list")
                    data["files"] = []
                    seen = set()
                    for ref in files:
                        if not isinstance(ref, dict):
                            raise ValueError("Invalid attachment reference")
                        _filename(ref.get("filename"))
                        if ref["filename"] in seen or ref.get("role", "") not in {"", "code", "data"}:
                            raise ValueError("Duplicate filename or invalid file role")
                        seen.add(ref["filename"])
                        path = ref.get("path", "")
                        digest = ref.get("sha256")
                        if not isinstance(digest, str) or path != f"files/{digest}" or path not in names:
                            raise ValueError("Missing attachment")
                        if path not in blobs:
                            content = archive.read(path)
                            if hashlib.sha256(content).hexdigest() != digest:
                                raise ValueError("Attachment checksum does not match")
                            blobs[path] = content
                        data["files"].append({"filename": ref["filename"], "role": ref.get("role", ""), "path": path})
                        file_count += 1
                graph.clear()
                graph.update(clean)
            if set(definitions) != used_definitions or set(names) != {"manifest.json", *blobs}:
                raise ValueError("Package contains unreferenced content")
            return manifest, blobs, {"name": str(manifest.get("name") or "Pipeline")[:200],
                                     "nodes": len(manifest["graph"]["nodes"]), "files": file_count,
                                     "definitions": len(definitions), "secret_parameters": secret_count}
    except (zipfile.BadZipFile, KeyError, TypeError, AttributeError, UnicodeError, RuntimeError, NotImplementedError, RecursionError) as exc:
        raise ValueError("Invalid or incomplete inLUMEN project package") from exc
