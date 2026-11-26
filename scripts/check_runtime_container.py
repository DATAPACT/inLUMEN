#!/usr/bin/env python3
"""Build a disposable Dagster bundle and verify non-root, read-only execution.
Run: PYTHONPATH=codegen codegen/.venv/bin/python scripts/test_runtime_container.py
Requires Docker and network access for pinned Dagster dependencies.
"""
import json, os, tempfile, shutil
from pathlib import Path
from app.deployment_validation import validate_dagster_project
root=Path(tempfile.mkdtemp(prefix="inlumen-runtime-smoke-"))
try:
 project=root/'dagster';package=project/'src/inlumen_dagster_project';package.mkdir(parents=True)
 (root/'bundle-manifest.json').write_text('{}')
 (project/'pyproject.toml').write_text('[project]\nname="runtime-smoke"\nversion="0.1"\ndependencies=["dagster==1.13.12"]\n')
 (package/'__init__.py').write_text('')
 (package/'definitions.py').write_text("""import dagster as dg
import os
from pathlib import Path
@dg.asset
def isolated_probe():
    assert os.geteuid() == 65532
    try:
        Path('/must-not-write').write_text('unexpected')
    except OSError:
        pass
    else:
        raise AssertionError('runtime root must be read only')
    Path('/workspace/outputs/probe.txt').write_text('non-root read-only runtime verified')
    return 'passed'
defs = dg.Definitions(assets=[isolated_probe])
""")
 (project/'Dockerfile').write_text('FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534\nRUN pip install --no-cache-dir dagster==1.13.12\nCOPY dagster /workspace/dagster\nENV PYTHONPATH=/workspace/dagster/src\n')
 report=validate_dagster_project(project,materialize=True,timeout_seconds=180)
 print(json.dumps({key: value for key, value in report.items() if key != 'steps'}, default=str))
 assert report['ok'], report.get('errors')
 assert (root/'outputs/probe.txt').read_text()=='non-root read-only runtime verified'
 print('REAL DAGSTER CONTAINER: passed')
finally:
 shutil.rmtree(root)
