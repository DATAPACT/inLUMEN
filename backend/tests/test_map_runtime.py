"""Parallel Map runtime: split -> per-item steps -> Collect, run like the engine runs steps."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inlumen.contracts.artifact_runtime import (  # noqa: E402
    assign_output_ports,
    prepare_node_environment,
    publish_port_artifacts,
    stage_bound_artifacts,
    validate_result,
)
from deployment_agents import _control_flow_runtime, _lift_map_scopes  # noqa: E402

CONTRACT_ID = "inlumen.generic-node@2"

# Per-item code: written for ONE invoice; fails on purpose for "broken".
EXTRACT_MAIN = '''
import json, os, sys, time
from pathlib import Path
inputs = json.loads(Path(os.environ["INLUMEN_INPUT_MANIFEST"]).read_text())["inputs"]
(item,) = inputs
text = Path(item["path"]).read_text()
if "broken" in text:
    sys.exit("cannot parse " + Path(item["path"]).name)
time.sleep(0.4)
out = Path(os.environ["PIPELINE_OUTPUT_DIR"]) / "fields.json"
out.write_text(json.dumps({"source": Path(item["path"]).name, "key": os.environ["INLUMEN_ITEM_KEY"],
                           "pid": os.getpid(), "started": time.time()}))
print("parsed", item["filename"])
'''


def extract_runtime():
    contract = {
        "contract_id": CONTRACT_ID, "version": "2",
        "inputs": [{"name": "item", "filename": "2/item.txt", "representation": "file", "kind": "text",
                    "connection_id": "2:item->3:input", "source_node": "2", "target_port": "input"}],
        "outputs": [{"name": "fields", "filename": "fields.json", "representation": "file", "kind": "json",
                     "format": "json", "port": "output"}],
    }
    manifest = {"flow_id": "3", "data_contract": contract}
    artifact = {"flow_id": "3", "data_contract": contract, "manifest": manifest, "files": [
        {"filename": "main.py", "content": EXTRACT_MAIN},
        {"filename": "node-manifest.json", "content": json.dumps(manifest)},
    ]}
    dockerfile = {"flow_id": "3", "command": ["python", "/app/main.py"], "files": ["main.py", "node-manifest.json"],
                  "content": 'FROM python:3.11-slim\nCOPY ["main.py", "/app/main.py"]\nCMD ["python", "/app/main.py"]\n',
                  "data_contract": contract}
    return artifact, dockerfile


def build(policy):
    steps = [
        {"flow_id": "2", "type": "flow", "template": "Parallel Map", "label": "Each Invoice",
         "param": {"max_concurrency": 3, "failure_policy": policy},
         "ports": {"inputs": [{"id": "items"}], "outputs": [{"id": "item"}]}},
        {"flow_id": "3", "type": "task", "label": "Extract Fields",
         "ports": {"inputs": [{"id": "input"}], "outputs": [{"id": "output"}]}},
        {"flow_id": "4", "type": "flow", "template": "Collect", "label": "Collect Invoices",
         "ports": {"inputs": [{"id": "item"}], "outputs": [{"id": "items"}]}},
    ]
    graph = {"edges": [
        {"source": "2", "sourceHandle": "item", "target": "3", "targetHandle": "input"},
        {"source": "3", "sourceHandle": "output", "target": "4", "targetHandle": "item"},
    ]}
    pairs = [_control_flow_runtime(steps[0]), extract_runtime(), _control_flow_runtime(steps[2])]
    artifacts, dockerfiles = [pair[0] for pair in pairs], [pair[1] for pair in pairs]
    errors = _lift_map_scopes(steps, graph, artifacts, dockerfiles)
    assert not errors, errors
    return {item["flow_id"]: (item, dockerfile, step) for item, dockerfile, step in zip(artifacts, dockerfiles, steps)}


def run_step(root, artifact, dockerfile, step, bindings):
    """What the engine's step runner does in strict mode."""
    node_dir = root / "nodes" / artifact["flow_id"]
    node_dir.mkdir(parents=True)
    for item in artifact["files"]:
        (node_dir / item["filename"]).write_text(item["content"])
    contract = artifact["data_contract"]
    input_dir, work_dir = root / "in" / artifact["flow_id"], root / "work" / artifact["flow_id"]
    output_dir, published = work_dir / "publish", root / "out" / artifact["flow_id"]
    bound = stage_bound_artifacts(bindings, input_dir) if bindings else []
    env = {**os.environ, **prepare_node_environment(input_dir, output_dir, work_dir, bound, contract, step.get("param") or {})}
    entry = node_dir / Path(dockerfile["command"][-1]).name
    process = subprocess.run([sys.executable, str(entry)], env=env, capture_output=True, text=True)
    declarations = assign_output_ports(contract["outputs"], [port["id"] for port in step["ports"]["outputs"]])
    if process.returncode == 0:
        validated = validate_result(output_dir, declarations, None, producer=artifact["flow_id"])
        publish_port_artifacts(output_dir, validated, published, declarations[0]["port"])
    return process, published, declarations


def binding(source_root, declaration, consumer_input, source_node):
    return {"source_dir": str(source_root / declaration["port"]), "artifact": declaration,
            "filename": consumer_input["filename"], "source_node": source_node,
            "connection_id": consumer_input.get("connection_id", ""), "target_port": consumer_input.get("target_port", "")}


class MapRuntimeTest(unittest.TestCase):
    def pipeline(self, policy, invoices):
        root = Path(tempfile.mkdtemp())
        source = root / "source"
        source.mkdir()
        for name, text in invoices.items():
            (source / name).write_text(text)
        built = build(policy)
        map_artifact, map_docker, map_step = built["2"]
        bundle_decl = {"name": "invoices", "filename": "invoices", "representation": "directory", "kind": "directory", "port": "data"}
        (root / "src" / "data").mkdir(parents=True)
        (source).rename(root / "src" / "data" / "invoices")
        _, map_out, map_decl = run_step(root, map_artifact, map_docker, map_step,
            [binding(root / "src", bundle_decl, {"filename": "1/invoices"}, "1")])
        extract, extract_docker, extract_step = built["3"]
        consumer_input = extract["data_contract"]["inputs"][0]
        extract_proc, extract_out, extract_decl = run_step(root, extract, extract_docker, extract_step,
            [binding(map_out, map_decl[0], consumer_input, "2")])
        return root, built, map_out, extract_proc, extract_out, extract_decl

    def test_per_item_step_runs_once_per_invoice_in_parallel_and_collect_gathers(self):
        root, built, map_out, process, extract_out, extract_decl = self.pipeline(
            "stop", {"a.txt": "invoice a", "b.txt": "invoice b", "c.txt": "invoice c"})
        self.assertEqual(0, process.returncode, process.stdout + process.stderr)
        self.assertEqual(["a", "b", "c"], sorted(path.name for path in (map_out / "item" / "items").iterdir() if path.is_dir()))
        results = extract_out / "output" / "fields_items"
        fields = {key: json.loads((results / key / "fields.json").read_text()) for key in ("a", "b", "c")}
        self.assertEqual({"a": "a.txt", "b": "b.txt", "c": "c.txt"}, {k: v["source"] for k, v in fields.items()})
        self.assertEqual(3, len({value["pid"] for value in fields.values()}))
        starts = sorted(value["started"] for value in fields.values())
        self.assertLess(starts[-1] - starts[0], 0.35, "items should run concurrently")
        self.assertIn("[b] parsed", process.stdout)

        collect, collect_docker, collect_step = built["4"]
        self.assertEqual("fields.json", collect["data_contract"]["outputs"][0]["item"]["filename"])
        collect_proc, collect_out, _ = run_step(root, collect, collect_docker, collect_step,
            [binding(extract_out, extract_decl[0], {"filename": "3/fields_items"}, "3")])
        self.assertEqual(0, collect_proc.returncode, collect_proc.stderr)
        gathered = collect_out / "items" / "collect_invoices_items"
        self.assertEqual(["a", "b", "c"], [item["key"] for item in json.loads((gathered / "_items.json").read_text())["items"]])
        self.assertTrue((gathered / "b" / "fields.json").is_file())

    def test_continue_policy_keeps_good_items_and_records_failures(self):
        root, built, _, process, extract_out, extract_decl = self.pipeline(
            "continue", {"a.txt": "invoice a", "bad.txt": "broken"})
        self.assertEqual(0, process.returncode, process.stderr)
        index = json.loads((extract_out / "output" / "fields_items" / "_items.json").read_text())["items"]
        self.assertEqual({"a": "succeeded", "bad": "failed"}, {item["key"]: item["status"] for item in index})
        self.assertIn("cannot parse", next(item for item in index if item["key"] == "bad")["error"])
        collect, collect_docker, collect_step = built["4"]
        _, collect_out, _ = run_step(root, collect, collect_docker, collect_step,
            [binding(extract_out, extract_decl[0], {"filename": "3/fields_items"}, "3")])
        gathered = collect_out / "items" / "collect_invoices_items"
        self.assertEqual(["a"], sorted(path.name for path in gathered.iterdir() if path.is_dir()))

    def test_stop_policy_fails_the_step(self):
        _, _, _, process, _, _ = self.pipeline("stop", {"a.txt": "invoice a", "bad.txt": "broken"})
        self.assertNotEqual(0, process.returncode)
        self.assertIn("item(s) failed: bad", process.stderr)

    def test_lifted_step_declares_collections_and_runs_the_item_runner(self):
        extract, docker, _ = build("stop")["3"]
        self.assertEqual(["python", "/app/inlumen_item_runner.py"], docker["command"])
        self.assertIn('COPY ["inlumen_item_runner.py", "/app/inlumen_item_runner.py"]', docker["content"])
        self.assertTrue(docker["content"].rstrip().endswith('CMD ["python", "/app/inlumen_item_runner.py"]'))
        (output,) = extract["data_contract"]["outputs"]
        self.assertEqual(("directory", "output", "fields.json"), (output["representation"], output["port"], output["item"]["filename"]))
        manifest = json.loads(next(f["content"] for f in extract["files"] if f["filename"] == "node-manifest.json"))
        self.assertEqual(extract["data_contract"], manifest["data_contract"])


if __name__ == "__main__":
    unittest.main()
