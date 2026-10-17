import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app import deployment_validation
from app.run_artifact_names import run_artifacts_bucket, run_output_key, step_receipt_key
from app.run_artifact_store import RunArtifactStore


class FakeClient:
    def __init__(self, fail=False):
        self.buckets = set()
        self.objects = {}
        self.fail = fail

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def fput_object(self, bucket, key, path, content_type="application/octet-stream"):
        if self.fail:
            raise ConnectionError("minio unavailable")
        self.objects[(bucket, key)] = (Path(path).read_bytes(), content_type)

    def put_object(self, bucket, key, data, length, content_type="application/octet-stream"):
        self.objects[(bucket, key)] = (data.read(length), content_type)


def write_run(root: Path) -> None:
    (root / "run-spec.json").write_text(json.dumps({"nodes": [
        {"id": "1", "label": "Clean", "output_path": "outputs/node-1", "outputs": [{"id": "output"}], "parents": []},
    ]}))
    scope = root / "outputs/node-1/run-uuid"
    (scope / "output").mkdir(parents=True)
    (scope / "output/clean.csv").write_text("a,b\n1,2\n")
    (scope / ".inlumen-step.json").write_text(json.dumps({
        "schema_version": "inlumen.step-receipt@1", "asset_key": "node_1_clean", "status": "succeeded",
        "inputs": [], "outputs": [{"path": "output/clean.csv", "sha256": "x"}],
    }))


def test_names_are_scoped_per_workspace_and_reject_unsafe_paths():
    assert run_artifacts_bucket("local-workspace") == "run-artifacts-local"
    assert run_artifacts_bucket("a") != run_artifacts_bucket("b")
    assert run_output_key("run-1", "outputs/n/a.csv") == "runs/run-1/outputs/n/a.csv"
    assert step_receipt_key("run-1", "7") == "runs/run-1/steps/7.json"
    for path in ("../x", "/abs", "a//b", ""):
        with pytest.raises(ValueError):
            run_output_key("run-1", path)
    with pytest.raises(ValueError):
        run_output_key("../run", "a")


def test_outputs_are_uploaded_and_returned_as_references(tmp_path):
    write_run(tmp_path)
    client = FakeClient()
    with patch.object(RunArtifactStore, "from_environment", return_value=RunArtifactStore(client)):
        response = deployment_validation._run_outputs_response(tmp_path, workspace_id="ws-1", run_id="run-1")

    bucket = run_artifacts_bucket("ws-1")
    [entry] = response["run_outputs"]
    assert entry["path"] == "outputs/node-1/clean.csv"
    assert (entry["storage"], entry["bucket"], entry["key"]) == ("minio", bucket, "runs/run-1/outputs/node-1/clean.csv")
    assert "content" not in entry
    assert entry["sha256"].startswith("sha256:") and entry["size_bytes"] == 8
    assert client.objects[(bucket, entry["key"])][0] == b"a,b\n1,2\n"
    [receipt] = response["step_receipts"]
    assert (receipt["flow_id"], receipt["node_label"], receipt["asset_key"]) == ("1", "Clean", "node_1_clean")
    assert json.loads(client.objects[(bucket, "runs/run-1/steps/1.json")][0])["flow_id"] == "1"


def test_outputs_stay_inline_without_storage_or_when_upload_fails(tmp_path):
    write_run(tmp_path)
    with patch.object(RunArtifactStore, "from_environment", return_value=None):
        inline = deployment_validation._run_outputs_response(tmp_path, workspace_id="ws", run_id="r")
    assert inline["run_outputs"][0]["content"] == "a,b\n1,2\n"
    assert "storage" not in inline["run_outputs"][0]
    assert all(".inlumen-step" not in item["path"] for item in inline["run_outputs"])

    with patch.object(RunArtifactStore, "from_environment", return_value=RunArtifactStore(FakeClient(fail=True))):
        fallback = deployment_validation._run_outputs_response(tmp_path, workspace_id="ws", run_id="r")
    assert fallback["run_outputs"][0]["content"] == "a,b\n1,2\n"
    assert "object storage failed" in fallback["artifact_storage_warning"]


def test_storage_is_configured_only_when_requested(monkeypatch):
    monkeypatch.delenv("INLUMEN_RUN_ARTIFACT_STORAGE", raising=False)
    assert RunArtifactStore.from_environment() is None
    monkeypatch.setenv("INLUMEN_RUN_ARTIFACT_STORAGE", "minio")
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    assert RunArtifactStore.from_environment() is None
    monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("MINIO_ROOT_USER", "user")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "password-123")
    monkeypatch.setenv("INLUMEN_RUN_ARTIFACT_RETENTION_DAYS", "7")
    store = RunArtifactStore.from_environment()
    assert store is not None and store.retention_days == 7
