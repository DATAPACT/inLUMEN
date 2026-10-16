"""A Task with several output ports hands each consumer only its own artifact."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import yaml

from async_runtime import run_async
from deployment_agents import generate_dockerfiles_with_agent
from deployment_artifacts import _ARGO_PORT_RUNNER, build_deployment_bundle_files

SPLIT = '''import json, os
from pathlib import Path
out = Path(os.environ["PIPELINE_OUTPUT_DIR"])
(out / "summary.json").write_text(json.dumps({"rows": 2}))
(out / "rows.csv").write_text("a\\n1\\n2\\n")
'''


def task(node_id, outputs, files):
    return {"id": node_id, "data": {
        "type": "task", "label": node_id, "implementation": {"kind": "python"},
        "ports": {"inputs": [{"id": "input", "name": "input"}],
                  "outputs": [{"id": port, "name": port} for port in outputs]},
        "file_buckets": [{"filename": name, "bucket": f"b-{node_id}", "role": "code"} for name in files]}}


def split_graph():
    manifest = {"version": 1, "outputs": [
        {"port": "summary", "type": "file", "path": "summary.json"},
        {"port": "rows", "type": "file", "path": "rows.csv", "format": "csv", "kind": "table"},
    ]}
    stored = {("b-split", "main.py"): SPLIT.encode(), ("b-split", "inlumen.task.json"): json.dumps(manifest).encode()}
    graph = {"nodes": [task("split", ["summary", "rows"], ["main.py", "inlumen.task.json"])], "edges": []}
    for port in ("summary", "rows"):
        node_id = f"use_{port}"
        graph["nodes"].append(task(node_id, ["output"], ["main.py", "inlumen.task.json"]))
        graph["edges"].append({"source": "split", "sourceHandle": port, "target": node_id, "targetHandle": "input"})
        stored[(f"b-{node_id}", "main.py")] = b"print('ok')\n"
        stored[(f"b-{node_id}", "inlumen.task.json")] = json.dumps({"version": 1, "output": {"type": "file", "path": "out.json"}}).encode()

    async def read(bucket, filename):
        return stored[(bucket, filename)]

    with patch("deployment_agents.read_minio_object_bytes", side_effect=read):
        payload = run_async(generate_dockerfiles_with_agent([], [], pipeline_graph=graph, require_attached_runtime=True))
    return graph, payload.model_dump()


def test_outputs_manifest_declares_one_artifact_per_port():
    _graph, payload = split_graph()
    split = next(item for item in payload["runtime_artifacts"] if item["flow_id"] == "split")
    assert [(item["port"], item["filename"]) for item in split["data_contract"]["outputs"]] == [
        ("summary", "summary.json"),
        ("rows", "rows.csv"),
    ]


def test_dagster_bindings_select_the_connected_port_artifact():
    graph, payload = split_graph()
    bundle = build_deployment_bundle_files(graph, payload, targets={"dagster": True, "argo": False})
    for port, filename in (("summary", "summary.json"), ("rows", "rows.csv")):
        defs = next(
            item for item in bundle["files"]
            if item["path"].endswith("defs.yaml") and f"use_{port}" in item["path"]
        )
        bindings = yaml.safe_load(defs["content"])["attributes"]["artifact_bindings"]
        assert [(item["source_port"], item["artifact"]["filename"]) for item in bindings] == [(port, filename)]


def test_argo_runner_publishes_each_output_under_its_port(tmp_path):
    _graph, payload = split_graph()
    split = next(item for item in payload["runtime_artifacts"] if item["flow_id"] == "split")
    script = tmp_path / "main.py"
    script.write_text(SPLIT)
    output = tmp_path / "published"
    result = subprocess.run([
        sys.executable, "-c", _ARGO_PORT_RUNNER,
        json.dumps([sys.executable, str(script)]), '["summary", "rows"]', "[]", "[]",
        '["summary", "rows"]', json.dumps(split["data_contract"]), "[]",
    ], env={**os.environ, "PIPELINE_INPUT_DIR": str(tmp_path / "input"), "PIPELINE_OUTPUT_DIR": str(output)},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert sorted(path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()) == [
        "rows/rows.csv",
        "summary/summary.json",
    ]


def test_output_declarations_are_not_runtime_parameters():
    from deployment_artifacts import _step_runtime_parameters
    from filesystem_runtime import parameter_environment

    param = {"threshold": 2, "output_artifacts": [{"port": "a", "filename": "a.json"}], "model_plan": {}}
    assert _step_runtime_parameters({"flow_id": "1", "param": param}) == {"threshold": 2}
    environment = parameter_environment(param)
    assert "output_artifacts" not in json.dumps(environment).lower()
