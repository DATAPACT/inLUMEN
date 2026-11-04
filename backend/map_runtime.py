"""Runtime for Parallel Map scopes: split, run per item, collect.

A Parallel Map publishes its input collection as one folder per item; every
step up to the matching Collect is *lifted*: its own code (written for one
item) is wrapped by a platform item runner that runs it once per item, up to
the map's max concurrency and under its failure policy, and publishes the
results as a collection again; the Collect gathers the successful items for
the steps after it.

Collection layout, shared by all three:

    <collection>/<item key>/<that item's artifact>
    <collection>/_items.json   {"items": [{"key", "status", "error"?}]}

The wrapper is the step's entrypoint, so the same mechanism works in every
engine (Dagster, Argo, local) without engine-specific mapping.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from map_scope import MapScope

CONTRACT_ID = "inlumen.generic-node@2"
ITEM_RUNNER_FILENAME = "inlumen_item_runner.py"
ITEMS_INDEX = "_items.json"
IDENTITY_FIELDS = ("name", "port", "source_node", "source_port", "connection_id", "target_port")
ITEM_FIELDS = ("name", "filename", "kind", "format", "representation", "schema", "columns",
               "required_columns", "members", "semantic_role")


def _artifact_runtime_source() -> str:
    return Path(__file__).with_name("artifact_runtime.py").read_text(encoding="utf-8")


def collection_name(label: object, fallback: str = "collected") -> str:
    """Same naming as codegen's Collect artifact (sanitize_fragment + snake case)."""
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(label or "")).strip(".-")
    return (text or fallback).replace("-", "_").lower() or fallback


def item_declaration(declaration: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(declaration[key]) for key in ITEM_FIELDS if key in declaration}


def map_items_output() -> dict[str, Any]:
    return {"name": "items", "filename": "items", "representation": "directory",
            "kind": "directory", "port": "item", "semantic_role": "map_items"}


def collect_output(label: object, item: dict[str, Any] | None = None) -> dict[str, Any]:
    name = collection_name(label)
    output = {"name": name, "filename": f"{name}_items", "representation": "directory",
              "kind": "directory", "port": "items", "semantic_role": "collection"}
    if item:
        output["item"] = item
    return output


def lift_contract(inner: dict[str, Any]) -> dict[str, Any]:
    """The contract a lifted step shows its engine: collections in, collections out."""
    def lifted(declaration: dict[str, Any], filename: str) -> dict[str, Any]:
        outer = {key: declaration[key] for key in IDENTITY_FIELDS if declaration.get(key)}
        return {**outer, "name": str(declaration.get("name") or Path(filename).stem),
                "filename": filename, "representation": "directory", "kind": "directory",
                "item": item_declaration(declaration)}

    used: set[str] = set()

    def output_filename(declaration: dict[str, Any]) -> str:
        base = collection_name(declaration.get("name") or Path(str(declaration.get("filename"))).stem, "output")
        candidate, suffix = f"{base}_items", 2
        while candidate in used:
            candidate, suffix = f"{base}_{suffix}_items", suffix + 1
        used.add(candidate)
        return candidate

    return {
        "contract_id": CONTRACT_ID,
        "version": "2",
        "inputs": [lifted(item, str(item["filename"])) for item in inner.get("inputs", [])],
        "outputs": [lifted(item, output_filename(item)) for item in inner.get("outputs", [])],
    }


def _replace_file(files: list[dict[str, Any]], filename: str, content: str, content_type: str) -> None:
    for item in files:
        if item.get("filename") == filename:
            item["content"] = content
            return
    files.append({"filename": filename, "content": content, "content_type": content_type})


def _with_contract(runtime_artifact: dict, dockerfile: dict, contract: dict) -> None:
    """Point every copy of a step's contract at ``contract``."""
    runtime_artifact["data_contract"] = contract
    dockerfile["data_contract"] = contract
    manifest = runtime_artifact.get("manifest")
    if not isinstance(manifest, dict):
        manifest = next(
            (json.loads(item["content"]) for item in runtime_artifact.get("files") or []
             if item.get("filename") == "node-manifest.json" and item.get("content")),
            {},
        )
    manifest = {**manifest, "data_contract": contract}
    runtime_artifact["manifest"] = manifest
    _replace_file(runtime_artifact.setdefault("files", []), "node-manifest.json",
                  json.dumps(manifest, indent=2) + "\n", "application/json")


def _entrypoint_filename(dockerfile: dict) -> str:
    command = dockerfile.get("command")
    if isinstance(command, list) and command and str(command[-1]).endswith(".py"):
        return PurePosixPath(str(command[-1])).name
    return "main.py"


def lift_step(runtime_artifact: dict, dockerfile: dict, scope: MapScope, settings: dict) -> None:
    """Wrap a per-item step so its engine runs it once per item of the collection."""
    inner = copy.deepcopy(runtime_artifact.get("data_contract") or {})
    if inner.get("contract_id") != CONTRACT_ID:
        raise ValueError(
            f"Node {runtime_artifact.get('flow_id')} runs once per item of a Parallel Map, "
            "so it needs a declared artifact contract (generate its code or add inlumen.task.json)."
        )
    entry = _entrypoint_filename(dockerfile)
    spec = {
        "flow_id": str(runtime_artifact.get("flow_id") or ""),
        "map": scope.map,
        "collect": scope.collect,
        "entrypoint": entry,
        "inner_contract": inner,
        "max_concurrency": max(1, int(settings.get("max_concurrency") or 1)),
        "failure_policy": "continue" if settings.get("failure_policy") == "continue" else "stop",
    }
    _replace_file(runtime_artifact.setdefault("files", []), ITEM_RUNNER_FILENAME,
                  item_runner_source(spec), "text/x-python;charset=utf-8")
    _with_contract(runtime_artifact, dockerfile, lift_contract(inner))
    runner_command = ["python", f"/app/{ITEM_RUNNER_FILENAME}"]
    runtime_artifact["entrypoint"] = runner_command
    dockerfile["command"] = runner_command
    files = [str(item) for item in dockerfile.get("files") or []]
    if ITEM_RUNNER_FILENAME not in files:
        dockerfile["files"] = [*files, ITEM_RUNNER_FILENAME]
    dockerfile["content"] = _dockerfile_with_runner(str(dockerfile.get("content") or ""), runner_command)
    runtime_artifact["map_scope"] = {"map": scope.map, "collect": scope.collect, "inner_entrypoint": entry}


def _dockerfile_with_runner(content: str, command: list[str]) -> str:
    copy_line = f"COPY {json.dumps([ITEM_RUNNER_FILENAME, '/app/' + ITEM_RUNNER_FILENAME])}"
    lines = content.rstrip("\n").splitlines()
    position = next(
        (index for index in range(len(lines) - 1, -1, -1)
         if lines[index].strip().upper().startswith(("CMD ", "ENTRYPOINT "))),
        None,
    )
    instruction = f"CMD {json.dumps(command)}"
    if position is None:
        lines.extend([copy_line, instruction])
    else:
        keyword = lines[position].strip().split(" ", 1)[0].upper()
        lines[position:position + 1] = [copy_line, f"{keyword} {json.dumps(command)}"]
    return "\n".join(lines) + "\n"


def set_collect_item(runtime_artifact: dict, dockerfile: dict, item: dict[str, Any]) -> None:
    contract = copy.deepcopy(runtime_artifact.get("data_contract") or {})
    for output in contract.get("outputs", []):
        output["item"] = item_declaration(item)
    _with_contract(runtime_artifact, dockerfile, contract)


# --- generated runtimes ------------------------------------------------------

_COMMON = r'''
import json
import os
import re
import shutil
from pathlib import Path

ITEMS_INDEX = "_items.json"
IGNORED = {ITEMS_INDEX, ".gitkeep", "input_manifest.json", "output_manifest.json"}


def _inputs():
    manifest = os.environ.get("INLUMEN_INPUT_MANIFEST", "")
    if manifest and Path(manifest).is_file():
        bound = json.loads(Path(manifest).read_text(encoding="utf-8")).get("inputs", [])
        if bound:
            return bound
    root = Path(os.environ["PIPELINE_INPUT_DIR"])
    if not root.is_dir():
        return []
    return [{"filename": path.name, "path": str(path)} for path in sorted(root.iterdir())
            if path.name not in IGNORED]


def _copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if Path(source).is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _item_keys(collection):
    """Item keys of a collection folder, in order; failed items are skipped."""
    index = Path(collection) / ITEMS_INDEX
    if index.is_file():
        items = json.loads(index.read_text(encoding="utf-8")).get("items", [])
        return [str(item["key"]) for item in items if item.get("status") == "succeeded"]
    return sorted(path.name for path in Path(collection).iterdir()
                  if path.is_dir() and path.name not in IGNORED)


def _item_artifact(collection, key, item=None):
    folder = Path(collection) / key
    expected = folder / str((item or {}).get("filename") or "")
    if (item or {}).get("filename") and expected.exists():
        return expected
    entries = [path for path in folder.iterdir() if path.name not in IGNORED]
    if len(entries) != 1:
        raise RuntimeError(f"Item {key!r} of {Path(collection).name} must hold exactly one artifact")
    return entries[0]


def _write_index(collection, statuses):
    collection.mkdir(parents=True, exist_ok=True)
    (collection / ITEMS_INDEX).write_text(json.dumps({"items": statuses}, indent=2), encoding="utf-8")


def _key(name, used):
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(name).stem).strip(".-") or "item"
    candidate, suffix = base, 2
    while candidate in used:
        candidate, suffix = f"{base}-{suffix}", suffix + 1
    used.add(candidate)
    return candidate
'''


def map_split_source() -> str:
    """Parallel Map: publish the input collection as one folder per item."""
    return _COMMON + r'''

def main():
    inputs = _inputs()
    if len(inputs) != 1:
        raise RuntimeError(f"Parallel Map takes one collection, received {len(inputs)} inputs")
    source = Path(inputs[0]["path"])
    output = Path(os.environ["PIPELINE_OUTPUT_DIR"]) / "items"
    output.mkdir(parents=True, exist_ok=True)
    statuses, used = [], set()
    if source.is_dir() and ((source / ITEMS_INDEX).is_file() or inputs[0].get("item")):
        # A Collect output: keep its item keys.
        for key in _item_keys(source):
            used.add(key)
            artifact = _item_artifact(source, key, inputs[0].get("item"))
            _copy(artifact, output / key / artifact.name)
            statuses.append({"key": key, "status": "succeeded"})
    else:
        members = (sorted(path for path in source.iterdir() if path.name not in IGNORED)
                   if source.is_dir() else [source])
        for member in members:
            key = _key(member.name, used)
            _copy(member, output / key / member.name)
            statuses.append({"key": key, "status": "succeeded"})
    _write_index(output, statuses)
    print(f"Parallel Map: {len(statuses)} item(s)")


if __name__ == "__main__":
    main()
'''


def collect_source(output: dict[str, Any]) -> str:
    """Collect: gather the successful per-item results into one collection."""
    return _COMMON + f"\nDECLARED = json.loads({json.dumps(json.dumps(output))})\n" + r'''

def main():
    inputs = _inputs()
    if len(inputs) != 1:
        raise RuntimeError(f"Collect gathers one per-item result, received {len(inputs)} inputs")
    source = Path(inputs[0]["path"])
    target = Path(os.environ["PIPELINE_OUTPUT_DIR"]) / DECLARED["filename"]
    target.mkdir(parents=True, exist_ok=True)
    index = source / ITEMS_INDEX
    statuses = json.loads(index.read_text(encoding="utf-8")).get("items", []) if index.is_file() else [
        {"key": key, "status": "succeeded"} for key in _item_keys(source)
    ]
    for status in statuses:
        if status.get("status") == "succeeded":
            artifact = _item_artifact(source, status["key"], inputs[0].get("item"))
            _copy(artifact, target / status["key"] / artifact.name)
    _write_index(target, statuses)
    failed = [item["key"] for item in statuses if item.get("status") != "succeeded"]
    print(f"Collect: {len(statuses) - len(failed)} item(s) gathered"
          + (f", {len(failed)} failed: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
'''


def item_runner_source(spec: dict[str, Any]) -> str:
    """Entrypoint of a lifted step: run its own code once per item."""
    return _artifact_runtime_source() + "\n" + _COMMON + (
        f"\nSPEC = json.loads({json.dumps(json.dumps(spec, sort_keys=True))})\n"
    ) + r'''
import subprocess
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

_print_lock = threading.Lock()


def _say(key, text):
    with _print_lock:
        for line in str(text).splitlines():
            print(f"[{key}] {line}", flush=True)


def _match(declaration, outer_inputs):
    for field in ("connection_id", "target_port", "source_node"):
        if declaration.get(field):
            found = [item for item in outer_inputs if item.get(field) == declaration[field]]
            if len(found) == 1:
                return found[0]
    if len(outer_inputs) == 1:
        return outer_inputs[0]
    raise RuntimeError(f"No collection matches input {declaration.get('filename')!r}")


def _run_item(key, outer_inputs, outer_outputs, inner, parameters, work_root, out_root):
    root = work_root / "items" / key
    if root.exists():
        shutil.rmtree(root)
    input_dir, output_dir, work_dir = root / "input", root / "publish", root / "work"
    for folder in (input_dir, output_dir, work_dir):
        folder.mkdir(parents=True)
    inner_inputs = []
    for declaration in inner.get("inputs", []):
        outer = _match(declaration, outer_inputs)
        artifact = _item_artifact(outer["path"], key, outer.get("item"))
        # Keep the item's own name: per-item code often uses it as identity.
        relative = str(Path(str(declaration["filename"])).parent / artifact.name).lstrip("./") or artifact.name
        target = input_dir / relative
        _copy(artifact, target)
        inner_inputs.append({
            **{field: outer[field] for field in ("source_node", "source_port", "connection_id", "target_port") if outer.get(field)},
            **{field: declaration[field] for field in ("name", "kind", "format", "representation", "schema", "columns") if field in declaration},
            "filename": relative,
            "path": str(target.resolve()),
        })
    (work_dir / "inputs.json").write_text(json.dumps({"inputs": inner_inputs}, indent=2), encoding="utf-8")
    (work_dir / "context.json").write_text(json.dumps(
        {"data_contract": inner, "parameters": parameters, "item_key": key}, indent=2), encoding="utf-8")
    env = {
        **os.environ,
        "PIPELINE_INPUT_DIR": str(input_dir.resolve()),
        "PIPELINE_OUTPUT_DIR": str(output_dir.resolve()),
        "PIPELINE_WORK_DIR": str(work_dir.resolve()),
        "INLUMEN_INPUT_MANIFEST": str((work_dir / "inputs.json").resolve()),
        "INLUMEN_OUTPUT_MANIFEST": str((work_dir / "outputs.json").resolve()),
        "INLUMEN_CONTEXT_PATH": str((work_dir / "context.json").resolve()),
        "INLUMEN_ITEM_KEY": key,
    }
    entry = Path(__file__).resolve().with_name(SPEC["entrypoint"])
    process = subprocess.run([sys.executable, str(entry)], env=env, capture_output=True, text=True)
    _say(key, process.stdout)
    if process.returncode != 0:
        _say(key, process.stderr)
        raise RuntimeError((process.stderr or process.stdout).strip()[-2000:] or f"exit code {process.returncode}")
    receipt = work_dir / "outputs.json"
    actual = json.loads(receipt.read_text(encoding="utf-8")).get("outputs") if receipt.is_file() else None
    validated = validate_result(output_dir, inner.get("outputs", []), actual, producer=f"{SPEC['flow_id']}[{key}]")
    for result in validated:
        outer = (outer_outputs[0] if len(outer_outputs) == 1
                 else next(item for item in outer_outputs if item.get("port") == result.get("port")))
        _copy(result["path"], out_root / outer["filename"] / key / Path(str(result["filename"])).name)


def main():
    context_path = os.environ.get("INLUMEN_CONTEXT_PATH") or str(Path(__file__).resolve().with_name("node-manifest.json"))
    context = json.loads(Path(context_path).read_text(encoding="utf-8"))
    outer = context.get("data_contract") or {}
    parameters = context.get("parameters") or {}
    inner = SPEC["inner_contract"]
    outer_inputs = _inputs()
    out_root = Path(os.environ["PIPELINE_OUTPUT_DIR"])
    work_root = Path(os.environ.get("PIPELINE_WORK_DIR") or out_root.parent / "work")
    keys = None
    for item in outer_inputs:
        available = _item_keys(item["path"])
        keys = available if keys is None else [key for key in keys if key in set(available)]
    keys = keys or []
    limit, policy = SPEC["max_concurrency"], SPEC["failure_policy"]
    print(f"Running {SPEC['flow_id']} for {len(keys)} item(s), up to {limit} at once ({policy} on failure)", flush=True)
    statuses = {key: {"key": key, "status": "pending"} for key in keys}
    pending, running = list(keys), {}
    stop = False
    with ThreadPoolExecutor(max_workers=limit) as pool:
        while (pending and not stop) or running:
            while pending and not stop and len(running) < limit:
                key = pending.pop(0)
                running[pool.submit(_run_item, key, outer_inputs, outer["outputs"], inner,
                                    parameters, work_root, out_root)] = key
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                key = running.pop(future)
                try:
                    future.result()
                    statuses[key]["status"] = "succeeded"
                except Exception as exc:
                    statuses[key].update(status="failed", error=str(exc)[-2000:])
                    _say(key, f"FAILED: {exc}")
                    stop = stop or policy == "stop"
    for key in pending:
        statuses[key]["status"] = "skipped"
    for declaration in outer["outputs"]:
        _write_index(out_root / declaration["filename"], list(statuses.values()))
    failed = [key for key, item in statuses.items() if item["status"] == "failed"]
    succeeded = [key for key, item in statuses.items() if item["status"] == "succeeded"]
    print(f"{SPEC['flow_id']}: {len(succeeded)} item(s) succeeded, {len(failed)} failed", flush=True)
    if failed and (policy == "stop" or not succeeded):
        raise SystemExit(f"{len(failed)} item(s) failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
'''
