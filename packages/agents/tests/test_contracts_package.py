"""The contracts module installs with its data files and stays importable."""

import ast

from inlumen.contracts import artifact_runtime_source, port_types, task_package_contract


def test_data_files_are_installed():
    assert port_types.SPEC
    assert task_package_contract.TASK_SCHEMA


def test_artifact_runtime_is_standalone():
    # Run bundles embed this file as a script, so it may only import the
    # standard library and must not use relative imports.
    tree = ast.parse(artifact_runtime_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "artifact_runtime.py must not use relative imports"
