from __future__ import annotations

import ast
import json
import re
import sys

from .schemas import GeneratedFile, RuntimeConstraints, ValidationReport

BANNED_IMPORTS = {
    "subprocess",
    "socket",
    "ftplib",
    "paramiko",
}
REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
IMPORT_PACKAGE_ALIASES = {
    "PIL": "pillow",
    "cv2": "opencv-python",
    "faster_whisper": "faster-whisper",
    "huggingface_hub": "huggingface-hub",
    "sklearn": "scikit-learn",
}


def validate_generated_files(
    *,
    files: list[GeneratedFile],
    runtime_constraints: RuntimeConstraints,
) -> ValidationReport:
    checks = [
        "required_files_present",
        "python_syntax",
        "unsafe_import_scan",
        "unsafe_call_scan",
        "dependency_allowlist",
        "manifest_json",
    ]
    errors: list[str] = []
    warnings: list[str] = []

    by_name = {item.filename: item for item in files}
    main_py = by_name.get("main.py")
    requirements = by_name.get("requirements.txt")
    manifest = by_name.get("node-manifest.json")

    for filename in ("main.py", "requirements.txt", "node-manifest.json"):
        if filename not in by_name:
            errors.append(f"Missing required generated file: {filename}")

    imported_roots: set[str] = set()
    if main_py is not None:
        try:
            tree = ast.parse(main_py.content, filename="main.py")
        except SyntaxError as exc:
            errors.append(f"main.py has invalid syntax: {exc}")
        else:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        imported_roots.add(root)
                        if root in BANNED_IMPORTS:
                            errors.append(f"main.py imports banned module: {root}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".", 1)[0]
                    imported_roots.add(root)
                    if root in BANNED_IMPORTS:
                        errors.append(f"main.py imports banned module: {root}")
                elif isinstance(node, ast.Call):
                    unsafe_call = unsafe_call_name(node.func)
                    if unsafe_call:
                        errors.append(
                            f"main.py calls banned function: {unsafe_call}"
                        )
        for required_token in ("PIPELINE_INPUT_DIR", "PIPELINE_OUTPUT_DIR"):
            if required_token not in main_py.content:
                errors.append(
                    f"main.py does not explicitly reference {required_token}"
                )

    if requirements is not None:
        allowed = {
            package_name(item)
            for item in runtime_constraints.allowed_packages
            if package_name(item)
        }
        declared: set[str] = set()
        for line in requirements.content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name = package_name(stripped)
            if not name:
                errors.append(f"Could not parse requirement: {stripped}")
                continue
            declared.add(name)
            if allowed and name not in allowed:
                errors.append(f"Requirement is not in the allowed package list: {name}")
        for import_root in sorted(imported_roots):
            requirement_name = requirement_name_for_import(import_root)
            if not requirement_name:
                continue
            if allowed and requirement_name not in allowed:
                continue
            if requirement_name not in declared:
                errors.append(
                    "main.py imports third-party package "
                    f"{import_root} but requirements.txt is missing {requirement_name}"
                )

    if manifest is not None:
        try:
            manifest_payload = json.loads(manifest.content)
        except json.JSONDecodeError as exc:
            errors.append(f"node-manifest.json is invalid JSON: {exc}")
        else:
            if not isinstance(manifest_payload, dict):
                errors.append("node-manifest.json must contain a JSON object")
            elif manifest_payload.get("schema_version") != 1:
                errors.append("node-manifest.json must declare schema_version=1")

    return ValidationReport(
        status="invalid" if errors else "valid",
        checks=checks,
        errors=errors,
        warnings=warnings,
    )


def package_name(requirement: str) -> str:
    match = REQUIREMENT_NAME_RE.match(requirement)
    if not match:
        return ""
    return match.group(1).lower().replace("_", "-")


def imported_roots_from_python(source: str) -> set[str]:
    try:
        tree = ast.parse(source, filename="main.py")
    except SyntaxError:
        return set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    return imported


def requirement_name_for_import(import_root: str) -> str:
    if not import_root:
        return ""
    if import_root in sys.stdlib_module_names:
        return ""
    return package_name(IMPORT_PACKAGE_ALIASES.get(import_root, import_root))


def unsafe_call_name(function: ast.expr) -> str:
    if isinstance(function, ast.Name) and function.id in {
        "eval",
        "exec",
        "__import__",
    }:
        return function.id
    if (
        isinstance(function, ast.Attribute)
        and function.attr == "system"
        and isinstance(function.value, ast.Name)
        and function.value.id == "os"
    ):
        return "os.system"
    return ""
