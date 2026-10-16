import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provenance_provo import build_run_prov_o_jsonld  # noqa: E402


def run_record():
    return {
        "run_id": "run-1",
        "status": "succeeded",
        "created_at": "2026-10-16T10:00:00Z",
        "finished_at": "2026-10-16T10:01:00Z",
        "result": {
            "status": "succeeded",
            "outputs": [
                {"path": "outputs/node-1/clean.csv", "filename": "clean.csv", "sha256": "sha256:aaa",
                 "size_bytes": 8, "source_node": "1", "port": "output"},
                {"path": "outputs/node-2/report.json", "filename": "report.json", "sha256": "sha256:bbb",
                 "size_bytes": 20, "source_node": "2", "port": "output"},
            ],
            "steps": [
                {"flow_id": "1", "node_label": "Clean", "status": "succeeded", "started_at": 1_790_000_000,
                 "finished_at": 1_790_000_010, "connections": [], "inputs": [{"path": "raw.csv", "sha256": "rrr"}],
                 "outputs": [{"path": "output/clean.csv", "sha256": "aaa", "size_bytes": 8}]},
                {"flow_id": "2", "node_label": "Report", "status": "succeeded", "started_at": 1_790_000_011,
                 "finished_at": 1_790_000_020,
                 "connections": [{"source_node": "1", "source_port": "output", "target_port": "input", "filename": "clean.csv"}],
                 "inputs": [{"path": "clean.csv", "sha256": "aaa", "size_bytes": 8}],
                 "outputs": [{"path": "output/report.json", "sha256": "bbb", "size_bytes": 20}]},
            ],
        },
    }


def by_id(document):
    return {node["@id"]: node for node in document["@graph"]}


def test_run_lineage_links_producer_outputs_to_consumer_inputs():
    document = build_run_prov_o_jsonld(run_record())
    nodes = by_id(document)
    run = "urn:inlumen:provenance:run:run-1"
    clean, report = f"{run}:step:1", f"{run}:step:2"
    artifact = "urn:inlumen:provenance:artifact:sha256:aaa"

    assert "inlumen:PipelineRun" in nodes[run]["@type"]
    assert nodes[report]["used"] == [artifact]
    assert nodes[report]["wasInformedBy"] == [clean]
    assert nodes[report]["partOf"] == run
    assert nodes[report]["startedAtTime"].endswith("Z")
    assert nodes[artifact]["wasGeneratedBy"] == clean
    assert nodes[artifact]["path"] == "outputs/node-1/clean.csv"
    assert nodes[artifact]["sizeBytes"] == 8
    raw = nodes["urn:inlumen:provenance:artifact:sha256:rrr"]
    assert "wasGeneratedBy" not in raw  # an uploaded source file, generated outside the run
    json.dumps(document)  # serialisable JSON-LD


def test_runs_without_step_receipts_still_attribute_outputs():
    record = run_record()
    record["result"].pop("steps")
    nodes = by_id(build_run_prov_o_jsonld(record))
    assert nodes["urn:inlumen:provenance:artifact:sha256:bbb"]["wasGeneratedBy"] == (
        "urn:inlumen:provenance:run:run-1:step:2"
    )


def test_prov_o_route_exports_run_lineage():
    import inlumen_api

    with patch.object(inlumen_api, "runner_request", return_value=run_record()) as runner:
        response = inlumen_api.app.test_client().get("/api/provenance/prov-o?run_id=run-1")
    assert response.status_code == 200
    assert response.mimetype == "application/ld+json"
    assert "inlumen-run-lineage-run-1.jsonld" in response.headers["Content-Disposition"]
    assert runner.call_args.args == ("GET", "/v1/pipeline-runs/run-1")
    assert any("inlumen:StepExecution" in node["@type"] for node in response.json["@graph"])
