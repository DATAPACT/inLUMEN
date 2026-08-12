from __future__ import annotations

import ast
import keyword
import re
from dataclasses import dataclass
from typing import Any

from .schemas import ValidationReport

ALLOWED_TOP_LEVEL = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Assign,
    ast.AnnAssign,
)
BANNED_IMPORTS = {"subprocess", "socket", "ftplib", "paramiko"}


class PipelineCompilerError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledNode:
    flow_id: str
    function_name: str
    source: str


def function_name_for_flow_id(flow_id: str) -> str:
    fragment = re.sub(r"\W+", "_", str(flow_id or "")).strip("_").lower()
    if not fragment:
        fragment = "node"
    if fragment[0].isdigit() or keyword.iskeyword(fragment):
        fragment = f"n_{fragment}"
    return f"node_{fragment}"


def validate_pipeline_source(
    source: str,
    node_functions: dict[str, str],
) -> ValidationReport:
    checks = [
        "pipeline_python_syntax",
        "pipeline_top_level_purity",
        "pipeline_node_function_contract",
        "pipeline_cross_node_isolation",
        "pipeline_unsafe_import_scan",
        "pipeline_unsafe_call_scan",
    ]
    errors: list[str] = []
    try:
        tree = ast.parse(source, filename="pipeline.py")
    except SyntaxError as exc:
        return ValidationReport(
            status="invalid",
            checks=checks,
            errors=[f"pipeline.py has invalid syntax: {exc}"],
        )

    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    node_names = set(node_functions.values())
    for statement in tree.body:
        if not isinstance(statement, ALLOWED_TOP_LEVEL):
            errors.append(
                "pipeline.py contains executable top-level statement: "
                f"{statement.__class__.__name__}"
            )
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            try:
                ast.literal_eval(value)
            except (ValueError, TypeError):
                target = (
                    ", ".join(ast.unparse(item) for item in statement.targets)
                    if isinstance(statement, ast.Assign)
                    else ast.unparse(statement.target)
                )
                expression = ast.unparse(value)
                errors.append(
                    "pipeline.py top-level assignment "
                    f"{target} at line {statement.lineno} is executable: "
                    f"{expression[:200]}. Move calls, comprehensions, and runtime "
                    "device/model initialization into the helper or node function "
                    "that uses them; module-level constants must be literal values."
                )
    for statement in ast.walk(tree):
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in statement.names]
                if isinstance(statement, ast.Import)
                else [statement.module or ""]
            )
            for name in names:
                root = name.split(".", 1)[0]
                if root in BANNED_IMPORTS:
                    errors.append(f"pipeline.py imports banned module: {root}")
        if isinstance(statement, ast.Call):
            if isinstance(statement.func, ast.Name) and statement.func.id in {
                "eval",
                "exec",
                "__import__",
            }:
                errors.append(f"pipeline.py calls banned function: {statement.func.id}")
            if (
                isinstance(statement.func, ast.Attribute)
                and isinstance(statement.func.value, ast.Name)
                and statement.func.value.id == "os"
                and statement.func.attr == "system"
            ):
                errors.append("pipeline.py calls banned function: os.system")

    for flow_id, function_name in node_functions.items():
        function = definitions.get(function_name)
        if function is None:
            errors.append(
                f"Pipeline node {flow_id} is missing function {function_name}"
            )
            continue
        if isinstance(function, ast.AsyncFunctionDef):
            errors.append(f"Pipeline node function {function_name} must be synchronous")
            continue
        positional = [*function.args.posonlyargs, *function.args.args]
        if [argument.arg for argument in positional] != [
            "inputs",
            "output_dir",
            "context",
        ]:
            errors.append(
                f"{function_name} must have signature (inputs, output_dir, context)"
            )
        referenced = {
            item.id
            for item in ast.walk(function)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
        }
        forbidden = sorted((referenced & node_names) - {function_name})
        if forbidden:
            errors.append(
                f"{function_name} directly calls other node functions: "
                f"{', '.join(forbidden)}"
            )

    return ValidationReport(
        status="invalid" if errors else "valid",
        checks=checks,
        errors=errors,
    )


def compile_pipeline_nodes(
    source: str,
    node_functions: dict[str, str],
) -> list[CompiledNode]:
    report = validate_pipeline_source(source, node_functions)
    if report.status == "invalid":
        raise PipelineCompilerError("; ".join(report.errors))
    tree = ast.parse(source, filename="pipeline.py")
    node_names = set(node_functions.values())
    definitions: dict[str, ast.stmt] = {}
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[statement.name] = statement

    compiled: list[CompiledNode] = []
    for flow_id, function_name in node_functions.items():
        target = definitions[function_name]
        shared_statements = transitive_shared_statements(
            tree.body,
            target,
            node_names,
        )
        module = ast.Module(
            body=[
                *shared_statements,
                target,
                *ast.parse(_node_runtime_adapter(function_name)).body,
            ],
            type_ignores=[],
        )
        ast.fix_missing_locations(module)
        compiled.append(
            CompiledNode(
                flow_id=flow_id,
                function_name=function_name,
                source=ast.unparse(module).strip() + "\n",
            )
        )
    return compiled


def isolate_pipeline_nodes(
    source: str,
    node_functions: dict[str, str],
    selected_flow_ids: set[str],
) -> str:
    """Return the canonical source needed to execute only selected nodes."""
    report = validate_pipeline_source(source, node_functions)
    if report.status == "invalid":
        raise PipelineCompilerError("; ".join(report.errors))
    unknown = selected_flow_ids - set(node_functions)
    if unknown:
        raise PipelineCompilerError(
            "Cannot isolate unknown pipeline nodes: " + ", ".join(sorted(unknown))
        )

    tree = ast.parse(source, filename="pipeline.py")
    node_names = set(node_functions.values())
    definitions = {
        statement.name: statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required_statement_ids: set[int] = set()
    selected_names = {
        node_functions[flow_id] for flow_id in selected_flow_ids
    }
    for function_name in selected_names:
        target = definitions[function_name]
        required_statement_ids.update(
            id(statement)
            for statement in transitive_shared_statements(
                tree.body,
                target,
                node_names,
            )
        )

    selected_statements = [
        statement
        for statement in tree.body
        if id(statement) in required_statement_ids
        or (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name in selected_names
        )
    ]
    module = ast.Module(body=selected_statements, type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module).strip() + "\n"


def transitive_shared_statements(
    body: list[ast.stmt],
    target: ast.stmt,
    node_names: set[str],
) -> list[ast.stmt]:
    """Select only top-level definitions needed by one node function."""
    providers: dict[str, int] = {}
    always_include: set[int] = set()
    for index, statement in enumerate(body):
        if isinstance(statement, ast.ImportFrom) and statement.module == "__future__":
            always_include.add(index)
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name in node_names
        ):
            continue
        for name in provided_names(statement):
            providers[name] = index

    required_indices = set(always_include)
    pending = list(loaded_names(target))
    visited_names: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited_names:
            continue
        visited_names.add(name)
        index = providers.get(name)
        if index is None or index in required_indices:
            continue
        statement = body[index]
        required_indices.add(index)
        pending.extend(loaded_names(statement))

    return [
        statement for index, statement in enumerate(body) if index in required_indices
    ]


def provided_names(statement: ast.stmt) -> set[str]:
    if isinstance(statement, ast.Import):
        return {
            alias.asname or alias.name.split(".", 1)[0] for alias in statement.names
        }
    if isinstance(statement, ast.ImportFrom):
        return {
            alias.asname or alias.name for alias in statement.names if alias.name != "*"
        }
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, ast.Assign):
        return {
            item.id
            for target in statement.targets
            for item in ast.walk(target)
            if isinstance(item, ast.Name)
        }
    if isinstance(statement, ast.AnnAssign):
        return {
            item.id for item in ast.walk(statement.target) if isinstance(item, ast.Name)
        }
    return set()


def loaded_names(node: ast.AST) -> set[str]:
    return {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }


def validate_compiled_equivalence(
    canonical_source: str,
    compiled_nodes: list[CompiledNode],
    node_functions: dict[str, str],
) -> ValidationReport:
    checks = [
        "compiled_node_ast_equivalence",
        "compiled_node_excludes_other_nodes",
    ]
    errors: list[str] = []
    canonical_tree = ast.parse(canonical_source, filename="pipeline.py")
    canonical_definitions = {
        node.name: node
        for node in canonical_tree.body
        if isinstance(node, ast.FunctionDef)
    }
    all_node_names = set(node_functions.values())
    for compiled in compiled_nodes:
        tree = ast.parse(compiled.source, filename=f"{compiled.flow_id}/main.py")
        definitions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        expected = canonical_definitions.get(compiled.function_name)
        actual = definitions.get(compiled.function_name)
        if expected is None or actual is None:
            errors.append(
                f"Compiled node {compiled.flow_id} is missing {compiled.function_name}."
            )
            continue
        if ast.dump(expected, include_attributes=False) != ast.dump(
            actual,
            include_attributes=False,
        ):
            errors.append(
                f"Compiled node {compiled.flow_id} changed the canonical "
                f"implementation of {compiled.function_name}."
            )
        leaked = sorted((set(definitions) & all_node_names) - {compiled.function_name})
        if leaked:
            errors.append(
                f"Compiled node {compiled.flow_id} contains other node "
                f"implementations: {', '.join(leaked)}"
            )
    return ValidationReport(
        status="invalid" if errors else "valid",
        checks=checks,
        errors=errors,
    )


def compose_pipeline_program(
    source: str,
    plan: dict[str, Any],
) -> str:
    report = validate_pipeline_source(
        source,
        {str(node["flow_id"]): str(node["function_name"]) for node in plan["nodes"]},
    )
    if report.status == "invalid":
        raise PipelineCompilerError("; ".join(report.errors))
    return (
        source.rstrip()
        + "\n\n"
        + f"INLUMEN_PIPELINE_PLAN = {plan!r}\n\n"
        + _pipeline_runtime_adapter()
        + "\n"
    )


def deterministic_pipeline_source(plan: dict[str, Any]) -> str:
    functions = []
    for node in plan["nodes"]:
        task_profile = node.get("task_profile") or {}
        implementation = node.get("implementation_plan") or {}
        materializer = (
            "_inlumen_train_classical_ml"
            if task_profile.get("name") == "model_training"
            and implementation.get("execution_profile") == "classical_ml"
            else "_inlumen_materialize"
        )
        functions.append(
            "\n".join(
                [
                    (f"def {node['function_name']}(inputs, output_dir, context):"),
                    (
                        f"    return {materializer}("
                        f"inputs, output_dir, {node['outputs']!r}, context)"
                    ),
                ]
            )
        )
    return _fallback_helpers().rstrip() + "\n\n" + "\n\n".join(functions) + "\n"


def _node_runtime_adapter(function_name: str) -> str:
    return f'''
def _inlumen_node_main():
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    _manifest_path = _Path(_os.environ["INLUMEN_INPUT_MANIFEST"])
    _output_dir = _Path(_os.environ["INLUMEN_OUTPUT_DIR"])
    _output_manifest_path = _Path(_os.environ["INLUMEN_OUTPUT_MANIFEST"])
    _context_path = _Path(_os.environ.get("INLUMEN_CONTEXT_PATH", ""))
    _manifest = _json.loads(_manifest_path.read_text(encoding="utf-8"))
    _inputs = _manifest.get("inputs") or _manifest.get("files") or []
    _context = {{}}
    if _context_path.is_file():
        _context = _json.loads(_context_path.read_text(encoding="utf-8"))
    _output_dir.mkdir(parents=True, exist_ok=True)
    _compat_dir = _output_dir / ".inlumen-inputs"
    _compat_dir.mkdir(parents=True, exist_ok=True)
    for _item in _inputs:
        if not isinstance(_item, dict):
            continue
        _source = _Path(str(_item.get("path") or ""))
        if not _source.exists():
            continue
        _filename = _Path(str(_item.get("filename") or _source.name))
        _aliases = [_Path(_filename.name)]
        if not _filename.is_absolute() and ".." not in _filename.parts:
            _aliases.append(_filename)
        for _alias in _aliases:
            _target = _compat_dir / _alias
            _target.parent.mkdir(parents=True, exist_ok=True)
            if not _target.exists():
                _target.symlink_to(_source)
    _previous_cwd = _Path.cwd()
    try:
        _os.chdir(_compat_dir)
        _outputs = {function_name}(_inputs, _output_dir, _context)
    finally:
        _os.chdir(_previous_cwd)
    if not isinstance(_outputs, list):
        raise TypeError("{function_name} must return a list of output descriptors")
    _data_contract = (
        _context.get("data_contract")
        if isinstance(_context.get("data_contract"), dict)
        else {{}}
    )
    _expected_outputs = (
        _data_contract.get("outputs")
        if isinstance(_data_contract.get("outputs"), list)
        else []
    )
    _normalized_outputs = []
    for _index, _item in enumerate(_outputs):
        if not isinstance(_item, dict):
            raise TypeError("{function_name} output descriptors must be objects")
        _expected = next(
            (
                _candidate for _candidate in _expected_outputs
                if isinstance(_candidate, dict)
                and (
                    (_item.get("name") and _candidate.get("name") == _item.get("name"))
                    or (
                        _item.get("filename")
                        and _candidate.get("filename") == _item.get("filename")
                    )
                )
            ),
            _expected_outputs[_index]
            if _index < len(_expected_outputs)
            and isinstance(_expected_outputs[_index], dict)
            else {{}},
        )
        _normalized = {{**_expected, **_item}}
        _raw_path = str(
            _normalized.get("path") or _normalized.get("filename") or ""
        )
        if not _raw_path:
            raise ValueError("{function_name} output is missing path and filename")
        _path = _Path(_raw_path)
        if not _path.is_absolute():
            _path = _output_dir / _path
        _normalized["path"] = str(_path)
        _normalized.setdefault("filename", _path.name)
        _normalized_outputs.append(_normalized)
    _outputs = _normalized_outputs
    _output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _output_manifest_path.write_text(
        _json.dumps({{
            "schema_version": "inlumen.output-manifest@1",
            "outputs": _outputs,
        }}, indent=2, sort_keys=True) + "\\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    _inlumen_node_main()
'''.strip()


def _pipeline_runtime_adapter() -> str:
    return """
def _inlumen_pipeline_load(path):
    import json as _json
    from pathlib import Path as _Path
    _path = _Path(path)
    if not path or not _path.is_file():
        return {}
    return _json.loads(_path.read_text(encoding="utf-8"))


def _inlumen_pipeline_main():
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    _manifest_path = _os.environ["INLUMEN_INPUT_MANIFEST"]
    _output_root = _Path(_os.environ["INLUMEN_OUTPUT_DIR"])
    _output_manifest_path = _Path(_os.environ["INLUMEN_OUTPUT_MANIFEST"])
    _context = _inlumen_pipeline_load(
        _os.environ.get("INLUMEN_CONTEXT_PATH", "")
    )
    _manifest = _inlumen_pipeline_load(_manifest_path)
    _root_inputs = _manifest.get("inputs") or _manifest.get("files") or []
    _produced = {}
    _nodes = INLUMEN_PIPELINE_PLAN["nodes"]
    for _node in _nodes:
        _flow_id = _node["flow_id"]
        _parents = _node.get("parents") or []
        _inputs = []
        for _parent in _parents:
            _inputs.extend(_produced.get(_parent, []))
        _filenames = set(_node.get("input_filenames") or [])
        _direct = [
            _item for _item in _root_inputs
            if not _filenames or _item.get("filename") in _filenames
        ]
        if not _parents:
            _inputs.extend(_direct or _root_inputs)
        else:
            _inputs.extend([
                _item for _item in _direct if _item not in _inputs
            ])
        _node_dir = _output_root / "nodes" / _flow_id
        _node_dir.mkdir(parents=True, exist_ok=True)
        _node_context = {
            **_context,
            "flow_id": _flow_id,
            "pipeline": INLUMEN_PIPELINE_PLAN.get("pipeline", {}),
            "node": _node.get("descriptor", {}),
        }
        _compat_dir = _node_dir / ".inlumen-inputs"
        _compat_dir.mkdir(parents=True, exist_ok=True)
        for _item in _inputs:
            if not isinstance(_item, dict):
                continue
            _source = _Path(str(_item.get("path") or ""))
            if not _source.exists():
                continue
            _filename = _Path(str(_item.get("filename") or _source.name))
            _aliases = [_Path(_filename.name)]
            if not _filename.is_absolute() and ".." not in _filename.parts:
                _aliases.append(_filename)
            for _alias in _aliases:
                _target = _compat_dir / _alias
                _target.parent.mkdir(parents=True, exist_ok=True)
                if not _target.exists():
                    _target.symlink_to(_source)
        _previous_cwd = _Path.cwd()
        try:
            _os.chdir(_compat_dir)
            _outputs = globals()[_node["function_name"]](
                _inputs, _node_dir, _node_context
            )
        finally:
            _os.chdir(_previous_cwd)
        if not isinstance(_outputs, list):
            raise TypeError(
                f"{_node['function_name']} must return a list of output descriptors"
            )
        _expected_outputs = (
            _node.get("outputs") if isinstance(_node.get("outputs"), list) else []
        )
        _normalized_outputs = []
        for _index, _item in enumerate(_outputs):
            if not isinstance(_item, dict):
                raise TypeError(
                    f"{_node['function_name']} output descriptors must be objects"
                )
            _expected = next(
                (
                    _candidate for _candidate in _expected_outputs
                    if isinstance(_candidate, dict)
                    and (
                        (_item.get("name") and _candidate.get("name") == _item.get("name"))
                        or (
                            _item.get("filename")
                            and _candidate.get("filename") == _item.get("filename")
                        )
                    )
                ),
                _expected_outputs[_index]
                if _index < len(_expected_outputs)
                and isinstance(_expected_outputs[_index], dict)
                else {},
            )
            _normalized = {**_expected, **_item}
            _path = _Path(
                str(_normalized.get("path") or _normalized.get("filename") or "")
            )
            if not _path.is_absolute():
                _path = _node_dir / _path
            _normalized["path"] = str(_path)
            _normalized.setdefault("filename", _path.name)
            if not _path.is_file() and not _path.is_dir():
                raise FileNotFoundError(
                    f"Node {_flow_id} declared missing output: {_path}"
                )
            _normalized_outputs.append(_normalized)
        _outputs = _normalized_outputs
        _produced[_flow_id] = _outputs
        (_node_dir / "output_manifest.json").write_text(
            _json.dumps({
                "schema_version": "inlumen.output-manifest@1",
                "flow_id": _flow_id,
                "outputs": _outputs,
            }, indent=2, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
    _parent_ids = {
        _parent
        for _node in _nodes
        for _parent in (_node.get("parents") or [])
    }
    _sink_ids = [
        _node["flow_id"] for _node in _nodes
        if _node["flow_id"] not in _parent_ids
    ]
    _final_outputs = [
        _item
        for _flow_id in _sink_ids
        for _item in _produced.get(_flow_id, [])
    ]
    _output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _output_manifest_path.write_text(
        _json.dumps({
            "schema_version": "inlumen.output-manifest@1",
            "outputs": _final_outputs,
        }, indent=2, sort_keys=True) + "\\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    _inlumen_pipeline_main()
""".strip()


def _fallback_helpers() -> str:
    return """
import csv
import json
import pickle
import shutil
from pathlib import Path


def _inlumen_default_json(schema, inputs, context):
    required = schema.get("required", []) if isinstance(schema, dict) else []
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    result = {"status": "generated", "input_count": len(inputs)}
    for key in required:
        spec = properties.get(key, {}) if isinstance(properties, dict) else {}
        kind = spec.get("type")
        if key == "metrics":
            result[key] = {"input_count": len(inputs)}
        elif key == "target_column":
            enum = spec.get("enum") if isinstance(spec, dict) else None
            result[key] = str(enum[0]) if enum else ""
        elif kind == "array":
            result[key] = []
        elif kind == "object":
            result[key] = {}
        elif kind == "number":
            result[key] = 0
        elif kind == "integer":
            result[key] = 0
        elif kind == "boolean":
            result[key] = False
        else:
            result[key] = ""
    return result


def _inlumen_json_satisfies_schema(path, schema):
    if not isinstance(schema, dict) or not schema:
        return True
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    required = schema.get("required", [])
    if schema.get("type") == "object" and not isinstance(value, dict):
        return False
    return not isinstance(required, list) or all(
        key in value for key in required
    )


def _inlumen_materialize(inputs, output_dir, specs, context):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for spec in specs:
        name = spec.get("name") or "output"
        file_format = spec.get("format") or "json"
        filename = spec.get("filename") or f"{name}.{file_format}"
        path = output_dir / filename
        matching = next(
            (
                item for item in inputs
                if item.get("kind") == spec.get("kind")
                and (
                    not spec.get("format")
                    or item.get("format") == spec.get("format")
                )
            ),
            None,
        )
        if matching and matching.get("path"):
            source = Path(str(matching["path"]))
            if (
                source.is_file()
                and (
                    file_format != "json"
                    or _inlumen_json_satisfies_schema(
                        source,
                        spec.get("schema") or {},
                    )
                )
            ):
                shutil.copy2(source, path)
        if not path.exists():
            if file_format in {"pickle", "pkl"}:
                with path.open("wb") as handle:
                    pickle.dump(
                        {"status": "generated", "input_count": len(inputs)},
                        handle,
                    )
            elif spec.get("kind") == "text":
                path.write_text(
                    "\\n".join(
                        str(item.get("sample") or item.get("filename") or "")
                        for item in inputs
                    ),
                    encoding="utf-8",
                )
            elif spec.get("kind") == "table" and file_format in {"csv", "tsv"}:
                delimiter = "\\t" if file_format == "tsv" else ","
                columns = spec.get("columns") or ["value"]
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=columns, delimiter=delimiter
                    )
                    writer.writeheader()
                    writer.writerow({column: "" for column in columns})
            else:
                path.write_text(
                    json.dumps(
                        _inlumen_default_json(
                            spec.get("schema") or {}, inputs, context
                        ),
                        indent=2,
                        sort_keys=True,
                    ) + "\\n",
                    encoding="utf-8",
                )
        outputs.append({
            **spec,
            "filename": filename,
            "path": str(path),
        })
    return outputs


def _inlumen_train_classical_ml(inputs, output_dir, specs, context):
    import importlib

    RandomForestClassifier = importlib.import_module(
        "sklearn.ensemble"
    ).RandomForestClassifier
    accuracy_score = importlib.import_module("sklearn.metrics").accuracy_score

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    tabular_input = next(
        (
            item for item in inputs
            if item.get("path")
            and str(item.get("format") or "").lower() in {"csv", "tsv"}
        ),
        None,
    )
    rows = []
    if tabular_input:
        source_path = Path(str(tabular_input["path"]))
        delimiter = "\t" if str(tabular_input.get("format")).lower() == "tsv" else ","
        with source_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=delimiter))
    if not rows:
        raise ValueError("Classical model training requires at least one tabular input row.")

    target_candidates = (
        "target", "label", "outcome", "risk", "risk_label",
        "abnormal_condition", "status", "alert",
    )
    columns = list(rows[0])
    target_column = next(
        (column for column in target_candidates if column in columns),
        "derived_risk_label",
    )
    feature_columns = [
        column for column in columns
        if column != target_column
        and column.lower() not in {"id", "patient_id", "device_id", "timestamp"}
        and any(
            str(row.get(column) or "").strip().replace(".", "", 1).replace("-", "", 1).isdigit()
            for row in rows
        )
    ]
    if not feature_columns:
        feature_columns = [columns[0]]

    features = []
    for row in rows:
        values = []
        for column in feature_columns:
            try:
                values.append(float(row.get(column) or 0))
            except (TypeError, ValueError):
                values.append(float(len(str(row.get(column) or ""))))
        features.append(values)

    if target_column in columns:
        raw_targets = [str(row.get(target_column) or "unknown") for row in rows]
    else:
        pivot = sorted(values[0] for values in features)[len(features) // 2]
        raw_targets = ["high" if values[0] > pivot else "normal" for values in features]

    estimator = RandomForestClassifier(n_estimators=64, random_state=42)
    estimator.fit(features, raw_targets)
    predictions = estimator.predict(features)
    accuracy = float(accuracy_score(raw_targets, predictions))

    outputs = []
    for spec in specs:
        name = spec.get("name") or "output"
        file_format = str(spec.get("format") or "json").lower()
        filename = spec.get("filename") or f"{name}.{file_format}"
        path = output_dir / filename
        if spec.get("kind") == "model" or file_format in {"pickle", "pkl", "joblib"}:
            with path.open("wb") as handle:
                pickle.dump(
                    {
                        "estimator": estimator,
                        "feature_columns": feature_columns,
                        "target_column": target_column,
                    },
                    handle,
                )
        else:
            payload = _inlumen_default_json(spec.get("schema") or {}, inputs, context)
            payload.update({
                "metrics": {"accuracy": accuracy, "training_rows": len(rows)},
                "target_column": target_column,
                "feature_columns": feature_columns,
            })
            path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\\n",
                encoding="utf-8",
            )
        outputs.append({**spec, "filename": filename, "path": str(path)})
    return outputs
""".strip()
