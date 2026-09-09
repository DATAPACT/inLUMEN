#!/usr/bin/env python3
"""Build the supplied-code example; optionally execute it in a real Dagster container.

Run with the backend environment:
    backend/.venv/bin/python scripts/verify_quickstart.py --materialize
Storage reads use the local example files. No application workspace is modified.
"""
import argparse
import asyncio
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from run_tests import project_python

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialize", action="store_true", help="Requires Docker and codegen dependencies.")
    args = parser.parse_args()

    from deployment_agents import generate_dockerfiles_with_agent
    from deployment_artifacts import build_deployment_bundle_files
    from pipeline_graph_validation import validate_pipeline_graph

    example = ROOT / "examples" / "order-summary"
    graph = json.loads((example / "pipeline.json").read_text())
    stored = {}
    for node_id, filename, role in (("orders", "orders.csv", "data"), ("summary", "main.py", "code")):
        bucket = f"files-step-id-{node_id}"
        node = next(node for node in graph["nodes"] if node["id"] == node_id)
        node["data"]["files"] = [{"filename": filename, "bucket": bucket, "role": role}]
        stored[(bucket, filename)] = (example / filename).read_bytes()
    validation = validate_pipeline_graph(graph)
    if not validation["valid"]:
        raise RuntimeError(validation["issues"])

    async def read_object(bucket, filename):
        return stored[(bucket, filename)]

    with patch("deployment_agents.read_minio_object_bytes", side_effect=read_object):
        runtime = asyncio.run(generate_dockerfiles_with_agent(
            [], [], pipeline_graph=graph, require_attached_runtime=True,
        ))
    bundle = build_deployment_bundle_files(graph, runtime.model_dump(), targets={"argo": False, "dagster": True})
    output = Path(tempfile.mkdtemp(prefix="inlumen-order-summary-"))
    for item in bundle["files"]:
        destination = output / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if item.get("content_encoding") == "base64":
            destination.write_bytes(base64.b64decode(item["content"]))
        else:
            destination.write_text(item.get("content", ""), encoding="utf-8")
    print(f"Generated bundle: {output}", flush=True)

    if args.materialize:
        result = subprocess.run([
            project_python(ROOT / "codegen"), "-m", "app.deployment_validation",
            str(output / "dagster"), "--dagster-only", "--timeout-seconds", "180",
        ], cwd=ROOT / "codegen", capture_output=True, text=True)
        (output / "verification.json").write_text(result.stdout, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Dagster verification failed; see {output / 'verification.json'}\n{result.stderr}")
        expected = json.loads((example / "expected-summary.json").read_text())
        results = list((output / "outputs").rglob("summary.json"))
        if not results or any(json.loads(path.read_text()) != expected for path in results):
            raise RuntimeError(f"Expected summary not produced: {results}")
        print(f"Verified real Dagster execution: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
