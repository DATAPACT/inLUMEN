"""Contracts shared by inLUMEN's agents and services.

- ``port_types``: port types and connection rules (rules in ``port_types.json``).
- ``map_scope``: Parallel Map and Collect scopes.
- ``artifact_runtime``: the artifact boundary used by generated code. It is
  also embedded as a standalone script in run bundles, so it must only import
  the standard library. Use :func:`artifact_runtime_source` to read it.
- ``task_package_contract``: Task package v1 (spec in ``task_package_spec.json``).
"""

from importlib.resources import files


def artifact_runtime_source() -> str:
    """Return ``artifact_runtime.py``'s source, for embedding in bundles."""
    return files(__name__).joinpath("artifact_runtime.py").read_text(encoding="utf-8")
