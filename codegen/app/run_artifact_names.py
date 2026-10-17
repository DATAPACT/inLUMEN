"""Object-storage names for pipeline run artifacts, shared by backend and codegen.

Run outputs live in one bucket per workspace, under ``runs/<run_id>/``. The
public output path is kept as the object key suffix, so a run's outputs and
their keys map one to one.
"""

import hashlib

LOCAL_WORKSPACE_ID = "local-workspace"
RUN_ARTIFACTS_PREFIX = "runs"


def run_artifacts_bucket(workspace_id):
    workspace_id = str(workspace_id or LOCAL_WORKSPACE_ID)
    if workspace_id == LOCAL_WORKSPACE_ID:
        return "run-artifacts-local"
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:20]
    return f"run-artifacts-ws-{digest}"


def run_prefix(run_id):
    run_id = str(run_id or "").strip()
    if not run_id or "/" in run_id or run_id in {".", ".."}:
        raise ValueError(f"Unsafe run id for object storage: {run_id!r}")
    return f"{RUN_ARTIFACTS_PREFIX}/{run_id}/"


def run_output_key(run_id, output_path):
    path = str(output_path or "").strip()
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe run output path: {output_path!r}")
    return run_prefix(run_id) + path


def step_receipt_key(run_id, flow_id):
    flow = str(flow_id or "").strip()
    if not flow or "/" in flow:
        raise ValueError(f"Unsafe step id: {flow_id!r}")
    return run_prefix(run_id) + f"steps/{flow}.json"
