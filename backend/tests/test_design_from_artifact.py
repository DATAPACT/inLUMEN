"""Designs built from a deployment bundle or an Argo workflow (issue #98)."""
import io
import json
import zipfile

import pytest

from export.design_from_artifact import (
    ArtifactError,
    design_from_argo_workflow,
    design_from_artifact,
    design_from_bundle_manifest,
)
from inlumen.contracts.pipeline_graph_validation import validate_pipeline_graph
from inlumen.contracts.project_ir import PROJECT_IR_SCHEMA_VERSION


def _port(port_id, name, kind="any"):
    return {"id": port_id, "name": name, "type": kind, "required": True, "description": ""}


def _manifest():
    return {
        "schema_version": "inlumen.deployment-bundle@3",
        "targets": {"argo": True, "dagster": False},
        "node_order": ["1", "2", "3", "4", "5"],
        "nodes": [
            {"flow_id": "5", "label": "Report", "type": "destination", "template": "File",
             "ports": {"inputs": [_port("data", "totals", "JSON")], "outputs": []}},
            {"flow_id": "1", "label": "Invoices", "type": "source", "template": "File",
             "ports": {"inputs": [], "outputs": [_port("data", "invoices", "Table")]}},
            {"flow_id": "2", "label": "For each invoice", "type": "flow", "template": "Parallel Map",
             "ports": {"inputs": [_port("items", "items")], "outputs": [_port("item", "item")]}},
            {"flow_id": "3", "label": "Extract", "type": "task", "template": "Task",
             "ports": {"inputs": [_port("input", "invoice")], "outputs": [_port("output", "fields", "JSON")]}},
            {"flow_id": "4", "label": "Collect", "type": "flow", "template": "Collect",
             "ports": {"inputs": [_port("item", "item")], "outputs": [_port("items", "items")]}},
        ],
        "connections": [
            {"source": "1", "source_port": "data", "target": "2", "target_port": "items"},
            {"source": "2", "source_port": "item", "target": "3", "target_port": "input"},
            {"source": "3", "source_port": "output", "target": "4", "target_port": "item"},
            {"source": "4", "source_port": "items", "target": "5", "target_port": "data"},
        ],
    }


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _pipeline(result):
    assert result["project"]["schema_version"] == PROJECT_IR_SCHEMA_VERSION
    return result["project"]["pipeline"]


def _canvas(result):
    """The Project JSON as a canvas graph, to validate what the canvas will hold."""
    pipeline = _pipeline(result)
    return {
        "nodes": [
            {"id": node["id"], "data": {
                "type": node["kind"], "label": node["label"], "description": node["description"],
                "template_label": node["template"]["name"],
                "ports": {"inputs": node["inputs"], "outputs": node["outputs"]},
            }}
            for node in pipeline["nodes"]
        ],
        "edges": [
            {"source": c["from"]["node"], "sourceHandle": c["from"]["port"],
             "target": c["to"]["node"], "targetHandle": c["to"]["port"]}
            for c in pipeline["connections"]
        ],
    }


def _errors(result):
    return [issue for issue in validate_pipeline_graph(_canvas(result))["issues"] if issue["severity"] == "error"]


def _links(result):
    return sorted(
        (c["from"]["node"], c["from"]["port"], c["to"]["node"], c["to"]["port"])
        for c in _pipeline(result)["connections"]
    )


def test_a_bundle_manifest_gives_back_steps_ports_and_connections():
    result = design_from_bundle_manifest(_manifest(), name="Invoices")
    nodes = {node["id"]: node for node in _pipeline(result)["nodes"]}

    assert result["kind"] == "inlumen-bundle" and result["step_count"] == 5
    assert [node["id"] for node in _pipeline(result)["nodes"]] == ["1", "2", "3", "4", "5"]  # node_order
    assert (nodes["2"]["kind"], nodes["2"]["template"]["name"]) == ("flow", "Parallel Map")
    assert (nodes["4"]["kind"], nodes["4"]["template"]["name"]) == ("flow", "Collect")
    assert nodes["3"]["outputs"][0]["type"] == "JSON" and nodes["3"]["inputs"][0]["name"] == "invoice"
    assert _links(result) == [
        ("1", "data", "2", "items"), ("2", "item", "3", "input"),
        ("3", "output", "4", "item"), ("4", "items", "5", "data"),
    ]
    assert _errors(result) == []
    # Steps are laid out left to right in the order of the connections.
    xs = [nodes[key]["position"]["x"] for key in "12345"]
    assert xs == sorted(xs) and len(set(xs)) == 5
    assert any("code and data files are not in the manifest" in note for note in result["notes"])


def test_a_bundle_zip_is_read_without_extracting_its_code():
    manifest = json.dumps(_manifest())
    for layout in ("bundle-manifest.json", "my-bundle/bundle-manifest.json"):
        data = _zip({layout: manifest, "nodes/node-3/main.py": "raise SystemExit('never run')"})
        result = design_from_artifact(data, filename="invoice-totals.zip")
        assert result["kind"] == "inlumen-bundle" and result["connection_count"] == 4
        assert result["project"]["project"]["name"] == "Invoice totals"
    # The manifest alone is accepted too.
    assert design_from_artifact(manifest.encode(), filename="bundle-manifest.json")["step_count"] == 5


@pytest.mark.parametrize("data, filename, message", [
    (b"", "empty.zip", "empty"),
    (b"PK\x03\x04 not a zip", "broken.zip", "cannot be read"),
    (_zip({"nodes/a/main.py": "x = 1"}), "code.zip", "no bundle-manifest.json"),
    (_zip({"a/b/bundle-manifest.json": "{}"}), "deep.zip", "no bundle-manifest.json"),
    (_zip({"bundle-manifest.json": "{not json"}), "bad.zip", "not valid JSON"),
    (_zip({"bundle-manifest.json": '{"schema_version": "other@1"}'}), "other.zip", "not an inLUMEN bundle manifest"),
    (b"just: some\nyaml: file\n", "values.yaml", "no Argo Workflow"),
    (b'{"schema_version": "inlumen.project@2"}', "project.json", "no Argo Workflow"),
    (b"\xff\xfe\x00binary", "blob.bin", "neither a ZIP nor a text file"),
    (b"a: [unclosed", "broken.yaml", "not valid YAML"),
])
def test_other_files_are_refused_with_a_reason(data, filename, message):
    with pytest.raises(ArtifactError, match=message):
        design_from_artifact(data, filename=filename)


def test_a_manifest_with_a_dangling_connection_keeps_the_rest():
    manifest = _manifest()
    manifest["connections"].append({"source": "9", "source_port": "x", "target": "5", "target_port": "data"})
    result = design_from_bundle_manifest(manifest)
    assert result["connection_count"] == 4
    assert any("1 connection(s) named a step that is not in the manifest" in note for note in result["notes"])


ARGO_DAG = """
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: train-model-
spec:
  entrypoint: main
  onExit: notify
  templates:
  - name: main
    dag:
      tasks:
      - name: fetch-data
        template: fetch
      - name: clean
        template: run
        dependencies: [fetch-data]
        arguments:
          artifacts:
          - name: raw
            from: "{{tasks.fetch-data.outputs.artifacts.dataset}}"
      - name: features
        template: run
        depends: "clean.Succeeded"
      - name: train
        template: run
        depends: "features && clean"
        when: "{{tasks.clean.outputs.parameters.rows}} > 100"
      - name: evaluate
        template: evaluate-all
        dependencies: [train]
        withItems: [a, b]
      - name: publish
        templateRef: {name: shared, template: upload}
        dependencies: [evaluate]
  - name: fetch
    container: {image: "alpine:3.20", command: [sh, -c, "wget ..."]}
  - name: run
    script: {image: "python:3.12", command: [python], source: "print(1)"}
  - name: evaluate-all
    steps:
    - - {name: a, template: run}
  - name: notify
    container: {image: alpine}
"""


def test_an_argo_dag_becomes_steps_and_connections():
    result = design_from_artifact(ARGO_DAG.encode(), filename="train.yaml")
    nodes = {node["label"]: node for node in _pipeline(result)["nodes"]}

    assert result["kind"] == "argo-workflow" and result["project"]["project"]["name"] == "Train model"
    assert [(node["label"], node["kind"]) for node in _pipeline(result)["nodes"]] == [
        ("Fetch data", "source"), ("Clean", "task"), ("Features", "task"),
        ("Train", "task"), ("Evaluate", "task"), ("Publish", "destination"),
    ]
    # The artifact argument names the input port; plain ordering uses the default port.
    assert _links(result) == [
        ("1", "data", "2", "raw"), ("2", "output", "3", "input"), ("2", "output", "4", "clean"),
        ("3", "output", "4", "features"), ("4", "output", "5", "input"), ("5", "output", "6", "data"),
    ]
    assert _errors(result) == []
    assert "image alpine:3.20" in nodes["Fetch data"]["description"]
    assert "Runs only when:" in nodes["Train"]["description"]
    assert "nested template “evaluate-all”" in nodes["Evaluate"]["description"]
    notes = " ".join(result["notes"])
    for expected in ("Conditions (when)", "Loops (withItems", "Nested workflows", "template from another file",
                     "exit handler “notify”", "every port has the type “any”"):
        assert expected in notes


def test_argo_steps_run_group_after_group():
    workflow = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "CronWorkflow", "metadata": {"name": "nightly-report"},
        "spec": {"schedule": "0 2 * * *", "workflowSpec": {
            "entrypoint": "report",
            "templates": [
                {"name": "report", "steps": [
                    [{"name": "extract", "template": "job"}],
                    [{"name": "stats", "template": "job"}, {"name": "charts", "template": "job"}],
                    [{"name": "send", "template": "job", "arguments": {"parameters": [
                        {"name": "summary", "value": "{{steps.stats.outputs.result}}"}]}}],
                ]},
                {"name": "job", "container": {"image": "ghcr.io/acme/report:1.4"}},
            ],
        }},
    }
    result = design_from_argo_workflow(workflow)

    assert _links(result) == [
        ("1", "data", "2", "input"), ("1", "data", "3", "input"),
        ("2", "output", "4", "summary"), ("3", "output", "4", "charts"),
    ]
    assert [node["kind"] for node in _pipeline(result)["nodes"]] == ["source", "task", "task", "destination"]
    assert _errors(result) == []


def test_argo_edge_cases():
    single = {"kind": "Workflow", "spec": {"entrypoint": "job", "templates": [{"name": "job", "container": {"image": "alpine"}}]}}
    result = design_from_argo_workflow(single)
    assert result["step_count"] == 1 and _pipeline(result)["nodes"][0]["kind"] == "task"

    reference = {"kind": "Workflow", "spec": {"workflowTemplateRef": {"name": "shared-pipeline"}}}
    with pytest.raises(ArtifactError, match="Import that template file"):
        design_from_argo_workflow(reference)

    with pytest.raises(ArtifactError, match="not an Argo Workflow"):
        design_from_argo_workflow({"kind": "Deployment"})

    two = "kind: ConfigMap\nmetadata: {name: x}\n---\n" + ARGO_DAG + "\n---\n" + ARGO_DAG
    notes = design_from_artifact(two.encode(), filename="all.yaml")["notes"]
    assert any("1 more workflow document(s)" in note for note in notes)


def test_the_endpoint_returns_project_json_and_stores_nothing(monkeypatch):
    import inlumen_api

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    client = inlumen_api.app.test_client()
    url = "/api/pipeline/design-from-artifact"

    response = client.post(url, data={"file": (io.BytesIO(ARGO_DAG.encode()), "train.yaml")},
                           content_type="multipart/form-data")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.json["kind"] == "argo-workflow" and response.json["step_count"] == 6
    assert response.json["project"]["schema_version"] == PROJECT_IR_SCHEMA_VERSION

    refused = client.post(url, data={"file": (io.BytesIO(b"hello: world\n"), "values.yaml")},
                          content_type="multipart/form-data")
    assert refused.status_code == 422 and "no Argo Workflow" in refused.json["details"]

    assert client.post(url, data={}, content_type="multipart/form-data").status_code == 400
