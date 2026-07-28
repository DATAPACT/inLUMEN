from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - service image installs PyYAML.
    yaml = None


def _dagster_project_root(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if (candidate / "pyproject.toml").is_file() and (candidate / "src").is_dir():
        return candidate
    child = candidate / "dagster_project"
    if (child / "pyproject.toml").is_file() and (child / "src").is_dir():
        return child.resolve()
    canonical_child = candidate / "dagster"
    if (canonical_child / "pyproject.toml").is_file() and (canonical_child / "src").is_dir():
        return canonical_child.resolve()
    raise FileNotFoundError(f"Could not find a generated dagster_project under {candidate}")


def _bundle_root(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if (candidate / "bundle-manifest.json").is_file():
        return candidate
    if candidate.name in {"dagster", "dagster_project"} and (candidate.parent / "bundle-manifest.json").is_file():
        return candidate.parent
    if candidate.suffix.lower() in {".yaml", ".yml"}:
        return candidate.parent
    return candidate


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command": command,
            "returncode": None,
            "output": str(exc),
            "ok": False,
        }
    return {
        "command": command,
        "returncode": completed.returncode,
        "output": completed.stdout,
        "ok": completed.returncode == 0,
    }


def _ensure_venv(project_root: Path, *, reinstall: bool, timeout_seconds: int) -> tuple[Path, list[dict[str, Any]]]:
    venv_dir = project_root / ".inlumen_dagster_validation_venv"
    steps: list[dict[str, Any]] = []
    if reinstall and venv_dir.exists():
        shutil.rmtree(venv_dir)
    if not venv_dir.exists():
        steps.append(_run([sys.executable, "-m", "venv", str(venv_dir)], cwd=project_root, timeout_seconds=timeout_seconds))
        if not steps[-1]["ok"]:
            return venv_dir, steps

    python = venv_dir / "bin" / "python"
    steps.append(_run([str(python), "-m", "pip", "install", "--upgrade", "pip"], cwd=project_root, timeout_seconds=timeout_seconds))
    if not steps[-1]["ok"]:
        return venv_dir, steps
    steps.append(_run([str(python), "-m", "pip", "install", "-e", "."], cwd=project_root, timeout_seconds=timeout_seconds))
    return venv_dir, steps


def _validation_script() -> str:
    return r'''
import json
import sys

import dagster as dg
from inlumen_dagster_project.definitions import defs

definitions = defs() if callable(defs) else defs
asset_keys = sorted(str(key.to_user_string()) for key in definitions.resolve_asset_graph().get_all_asset_keys())
print(json.dumps({"asset_keys": asset_keys}, indent=2))
'''


def _materialize_script() -> str:
    return r'''
import dagster as dg
from inlumen_dagster_project.definitions import defs

definitions = defs() if callable(defs) else defs
assets = list(definitions.assets or [])
resources = dict(definitions.resources or {})
result = dg.materialize(assets, resources=resources, raise_on_error=False)
if not result.success:
    raise SystemExit(1)
'''


def validate_dagster_project(
    path: Path,
    *,
    materialize: bool = True,
    reinstall: bool = False,
    skip_install: bool = False,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    project_root = _dagster_project_root(path)
    report: dict[str, Any] = {
        "project_root": str(project_root),
        "ok": False,
        "steps": [],
    }

    required_files = [
        "pyproject.toml",
        "config/dagster.yaml",
        "src/inlumen_dagster_project/definitions.py",
        "src/inlumen_dagster_project/defs/__init__.py",
        "src/inlumen_dagster_project/components/node_runner.py",
        "src/inlumen_dagster_project/components/shell_command.py",
    ]
    missing = [item for item in required_files if not (project_root / item).is_file()]
    if missing:
        report["errors"] = [f"Missing required file: {item}" for item in missing]
        return report

    venv_dir = project_root / ".inlumen_dagster_validation_venv"
    if skip_install:
        if not (venv_dir / "bin" / "python").is_file():
            report["errors"] = [
                "skip_install was requested, but no validation venv exists. "
                "Run once without --skip-install."
            ]
            return report
    else:
        venv_dir, install_steps = _ensure_venv(
            project_root,
            reinstall=reinstall,
            timeout_seconds=timeout_seconds,
        )
        report["steps"].extend(install_steps)
        if any(not step["ok"] for step in install_steps):
            report["errors"] = ["Generated Dagster project installation failed."]
            return report

    python = venv_dir / "bin" / "python"
    env = {
        **os.environ,
        "DAGSTER_HOME": str(project_root / ".dagster_home"),
        "PYTHONPATH": str(project_root / "src"),
        "PATH": f"{venv_dir / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    Path(env["DAGSTER_HOME"]).mkdir(parents=True, exist_ok=True)

    for command in (
        [str(venv_dir / "bin" / "dg"), "check", "toml"],
        [str(venv_dir / "bin" / "dg"), "check", "yaml"],
    ):
        dg_check_step = _run(
            command,
            cwd=project_root,
            timeout_seconds=timeout_seconds,
            env=env,
        )
        report["steps"].append(dg_check_step)
        if not dg_check_step["ok"]:
            report["errors"] = [
                "Generated Dagster project failed the current dg project checks."
            ]
            return report

    load_step = _run(
        [str(python), "-c", _validation_script()],
        cwd=project_root,
        timeout_seconds=timeout_seconds,
        env=env,
    )
    report["steps"].append(load_step)
    if not load_step["ok"]:
        report["errors"] = ["Dagster definitions failed to load."]
        return report

    try:
        report["assets"] = json.loads(load_step["output"]).get("asset_keys", [])
    except Exception:
        report["assets"] = []

    if materialize:
        materialize_step = _run(
            [str(python), "-c", _materialize_script()],
            cwd=project_root,
            timeout_seconds=timeout_seconds,
            env=env,
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
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validate_bundle_structure(bundle_root: Path, targets: dict[str, Any]) -> dict[str, Any]:
    required_paths = [
        "bundle-manifest.json",
        "inputs/input_manifest.json",
        "nodes",
        "outputs",
    ]
    if targets.get("argo"):
        required_paths.append("argo/workflow.yaml")
    if targets.get("dagster"):
        required_paths.extend([
            "dagster/pyproject.toml",
            "dagster/config/dagster.yaml",
            "dagster/Dockerfile",
            "dagster/docker-compose.yml",
            "dagster/src/inlumen_dagster_project/definitions.py",
        ])

    missing = []
    for item in required_paths:
        candidate = bundle_root / item
        if not candidate.exists():
            missing.append(item)

    manifest = _load_json(bundle_root / "bundle-manifest.json")
    node_entries = manifest.get("nodes") if isinstance(manifest.get("nodes"), list) else []
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
    errors.extend(f"Missing node output directory: {item}" for item in missing_node_outputs)
    return {
        "ok": not errors,
        "bundle_root": str(bundle_root),
        "manifest_schema": manifest.get("schema_version"),
        "node_count": len(node_entries),
        "errors": errors,
    }


def _write_json_if_changed(path: Path, payload: dict[str, Any], actions: list[str]) -> None:
    next_text = json.dumps(payload, indent=2) + "\n"
    current_text = path.read_text(encoding="utf-8") if path.exists() else ""
    if current_text != next_text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(next_text, encoding="utf-8")
        actions.append(f"wrote {path.relative_to(_bundle_root(path)) if path.is_absolute() else path}")


def _write_text_if_missing(path: Path, content: str, actions: list[str]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    actions.append(f"created {path.name}")


def _safe_node_entries(manifest: dict[str, Any], bundle_root: Path) -> list[dict[str, Any]]:
    raw_nodes = manifest.get("nodes") if isinstance(manifest.get("nodes"), list) else []
    nodes = [node for node in raw_nodes if isinstance(node, dict)]
    if nodes:
        return nodes

    node_root = bundle_root / "nodes"
    if not node_root.is_dir():
        return []
    inferred = []
    for index, node_dir in enumerate(sorted(item for item in node_root.iterdir() if item.is_dir()), start=1):
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
    manifest = _load_json(manifest_path)
    raw_entries = manifest.get("inputs")
    if not isinstance(raw_entries, list):
        raw_entries = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    inputs = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        normalized = dict(entry)
        filename = str(normalized.get("filename") or normalized.get("name") or "").strip()
        if not filename:
            continue
        normalized["filename"] = filename
        normalized["path"] = f"inputs/{filename}"
        normalized.setdefault("kind", "file")
        normalized.setdefault("format", Path(filename).suffix.lstrip(".") or "text")
        inputs.append(normalized)

    known_filenames = {entry["filename"] for entry in inputs}
    for input_file in sorted(item for item in input_dir.iterdir() if item.is_file() and item.name != "input_manifest.json"):
        if input_file.name in known_filenames:
            continue
        inputs.append(
            {
                "filename": input_file.name,
                "path": f"inputs/{input_file.name}",
                "kind": "file",
                "format": input_file.suffix.lstrip(".") or "text",
                "description": "Sample input file copied from InLumen.",
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
    image: inlumen-generated-dagster:local
    init: true
    ports:
      - "3000:3000"
    volumes:
      - ./outputs:/workspace/outputs
      - dagster_home:/workspace/dagster/.dagster_home
      - ./dagster/config/dagster.yaml:/workspace/dagster/.dagster_home/dagster.yaml:ro
    environment:
      DAGSTER_HOME: /workspace/dagster/.dagster_home
      PYTHONUNBUFFERED: "1"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:3000/', timeout=2)"]
      interval: 5s
      timeout: 3s
      retries: 12
      start_period: 10s

volumes:
  dagster_home:
"""


def _compose_dagster_content() -> str:
    return """services:
  dagster:
    build:
      context: ..
      dockerfile: dagster/Dockerfile
    image: inlumen-generated-dagster:local
    init: true
    ports:
      - "3000:3000"
    volumes:
      - ../outputs:/workspace/outputs
      - dagster_home:/workspace/dagster/.dagster_home
      - ./config/dagster.yaml:/workspace/dagster/.dagster_home/dagster.yaml:ro
    environment:
      DAGSTER_HOME: /workspace/dagster/.dagster_home
      PYTHONUNBUFFERED: "1"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:3000/', timeout=2)"]
      interval: 5s
      timeout: 3s
      retries: 12
      start_period: 10s

volumes:
  dagster_home:
"""


def _is_runtime_attachment(path: Path) -> bool:
    basename = path.name.lower()
    return (
        basename
        in {
            "requirements.txt",
            "node-manifest.json",
            "validation-report.json",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "pyproject.toml",
            "poetry.lock",
        }
        or basename.startswith("dockerfile.")
        or path.suffix.lower()
        in {
            ".py",
            ".pyi",
            ".sh",
            ".bash",
            ".js",
            ".mjs",
            ".cjs",
            ".ts",
            ".tsx",
            ".ipynb",
        }
    )


def _repair_entry_script(node_dir: Path, current_script_path: str) -> Path | None:
    scripts = sorted(path for path in node_dir.rglob("*.py") if path.is_file())
    if not scripts:
        return None
    current_name = Path(current_script_path).name
    current = next((path for path in scripts if path.name == current_name), None)
    if current is not None:
        return current
    preferred = ("main.py", "app.py", "run.py", "process.py", "pipeline.py", "script.py")
    by_name = {path.name.lower(): path for path in scripts}
    return next((by_name[name] for name in preferred if name in by_name), scripts[0])


def _repair_dagster_defs(bundle_root: Path, nodes: list[dict[str, Any]], actions: list[str]) -> None:
    defs_root = bundle_root / "dagster" / "src" / "inlumen_dagster_project" / "defs"
    if yaml is None or not defs_root.is_dir() or not nodes:
        return

    node_by_flow = {str(node.get("flow_id") or ""): node for node in nodes}
    node_order = [str(node.get("flow_id") or "") for node in nodes]
    defs_files = sorted(defs_root.glob("*/defs.yaml"))
    for index, defs_file in enumerate(defs_files):
        try:
            parsed = yaml.safe_load(defs_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        attrs = parsed.get("attributes") if isinstance(parsed.get("attributes"), dict) else {}
        if not attrs:
            continue
        flow_id = node_order[index] if index < len(node_order) else ""
        node = node_by_flow.get(flow_id)
        if node is None:
            continue
        node_path = str(node.get("path") or "").strip()
        output_path = str(node.get("output_path") or "").strip()
        node_dir = bundle_root / node_path
        parents = [str(parent) for parent in node.get("parents") or []]
        first_parent = node_by_flow.get(parents[0]) if parents else None
        entry_script = _repair_entry_script(
            node_dir,
            str(attrs.get("script_path") or ""),
        )
        if entry_script is not None:
            relative_script = entry_script.relative_to(node_dir).as_posix()
            attrs["script_path"] = f"../{node_path}/{relative_script}"
        attrs["source_dir"] = f"../{node_path}"
        attrs["input_manifest_path"] = (
            f"../{first_parent.get('output_path')}/output_manifest.json"
            if first_parent
            else "../inputs/input_manifest.json"
        )
        attrs["output_dir"] = f"../{output_path}"
        attrs["output_manifest_path"] = f"../{output_path}/output_manifest.json"
        manifest_path = node_dir / "node-manifest.json"
        attrs["context_path"] = (
            f"../{node_path}/node-manifest.json"
            if manifest_path.is_file()
            else ""
        )
        attrs["local_input_paths"] = [
            f"../{node_path}/{path.relative_to(node_dir).as_posix()}"
            for path in sorted(node_dir.rglob("*"))
            if path.is_file() and not _is_runtime_attachment(path)
        ]
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
    manifest_targets = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
    selected_targets = {
        "argo": bool((targets or {}).get("argo", manifest_targets.get("argo", (bundle_root / "argo").exists()))),
        "dagster": bool((targets or {}).get("dagster", manifest_targets.get("dagster", (bundle_root / "dagster").exists() or (bundle_root / "dagster_project").exists()))),
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

    manifest["schema_version"] = "inlumen.deployment-bundle@1"
    manifest["targets"] = selected_targets
    manifest["node_order"] = [str(node.get("flow_id") or "") for node in repaired_nodes if str(node.get("flow_id") or "")]
    manifest["nodes"] = repaired_nodes
    manifest["inputs"] = {
        "path": "inputs",
        "manifest": "inputs/input_manifest.json",
        "sample_file_count": len(_load_json(bundle_root / "inputs" / "input_manifest.json").get("inputs") or []),
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
        _write_text_if_missing(bundle_root / "docker-compose.yml", _compose_root_content(), actions)
        _write_text_if_missing(bundle_root / "dagster" / "docker-compose.yml", _compose_dagster_content(), actions)
        _write_text_if_missing(
            bundle_root / "dagster" / "config" / "dagster.yaml",
            "{}\n",
            actions,
        )
        _repair_dagster_defs(bundle_root, repaired_nodes, actions)

    next_manifest = json.dumps(manifest, indent=2) + "\n"
    if not manifest_path.exists() or manifest_path.read_text(encoding="utf-8") != next_manifest:
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
    )
    return {
        "ok": bool(repair_report.get("ok") and validation_report.get("ok")),
        "bundle_root": validation_report.get("bundle_root") or repair_report.get("bundle_root"),
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
        docs = [doc for doc in yaml.safe_load_all(workflow_path.read_text(encoding="utf-8")) if doc is not None]
    except Exception as exc:
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
    if workflow.get("kind") not in {"Workflow", "WorkflowTemplate", "ClusterWorkflowTemplate", "CronWorkflow"}:
        errors.append("kind must be Workflow, WorkflowTemplate, ClusterWorkflowTemplate, or CronWorkflow")
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
            tasks = ((template.get("dag") or {}).get("tasks") or [])
            if not isinstance(tasks, list):
                errors.append(f"template '{name}' dag.tasks must be a list")
                continue
            task_names = {
                str(task.get("name") or "")
                for task in tasks
                if isinstance(task, dict)
            }
            for task in tasks:
                if not isinstance(task, dict):
                    errors.append(f"template '{name}' has non-object DAG task")
                    continue
                task_name = str(task.get("name") or "")
                task_template = str(task.get("template") or "")
                if not task_name or not task_template:
                    errors.append(f"template '{name}' DAG task requires name and template")
                if task_template and task_template not in template_by_name:
                    errors.append(f"task '{task_name}' references missing template '{task_template}'")
                for dependency in task.get("dependencies") or []:
                    if str(dependency) not in task_names:
                        errors.append(f"task '{task_name}' references missing dependency '{dependency}'")
        elif not any(key in template for key in ("container", "script", "steps", "resource", "suspend", "http")):
            errors.append(f"template '{name}' does not define an executable or control body")

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


def _run_argo_command(command: list[str], *, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
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
) -> dict[str, Any]:
    bundle_root = _bundle_root(path)
    manifest = _load_json(bundle_root / "bundle-manifest.json")
    manifest_targets = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
    selected_targets = {
        "argo": bool((targets or {}).get("argo", manifest_targets.get("argo", (bundle_root / "argo").exists()))),
        "dagster": bool((targets or {}).get("dagster", manifest_targets.get("dagster", (bundle_root / "dagster").exists() or (bundle_root / "dagster_project").exists()))),
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
            report["errors"].extend(argo_report.get("errors") or ["Argo validation failed."])

    if validate_dagster:
        dagster_report = validate_dagster_project(
            bundle_root,
            materialize=materialize,
            reinstall=reinstall,
            skip_install=skip_install,
            timeout_seconds=timeout_seconds,
        )
        report["dagster"] = dagster_report
        if not dagster_report.get("ok"):
            report["errors"].extend(dagster_report.get("errors") or ["Dagster validation failed."])

    report["ok"] = not report["errors"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an InLumen generated deployment bundle.")
    parser.add_argument("bundle_path")
    parser.add_argument("--target", choices=["argo", "dagster"], action="append", default=[])
    parser.add_argument("--dagster-only", action="store_true", help="Run the legacy Dagster project validator only.")
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
        targets = {
            "argo": "argo" in args.target,
            "dagster": "dagster" in args.target,
        } if args.target else None
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
