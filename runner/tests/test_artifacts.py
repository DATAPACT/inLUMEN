import base64

import pytest

from app.artifacts import PipelineArtifactStore


def test_artifact_store_keeps_bundle_and_output_bytes_outside_lifecycle_rows(tmp_path):
    store = PipelineArtifactStore(tmp_path)
    reference = store.store_bundle(
        "run-1",
        [{"path": "nodes/task/main.py", "content": "print('ok')\n"}],
    )
    assert store.load_bundle(reference)[0]["path"] == "nodes/task/main.py"

    outputs = store.store_outputs(
        "run-1",
        [
            {
                "path": "outputs/task/result.bin",
                "filename": "result.bin",
                "content": base64.b64encode(b"result").decode(),
                "content_encoding": "base64",
            }
        ],
    )
    assert "content" not in outputs[0]
    assert store.read_output(outputs[0]["_storage_path"]) == b"result"


def test_artifact_store_rejects_path_traversal(tmp_path):
    store = PipelineArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.store_outputs(
            "run-1",
            [{"path": "../escape.txt", "content": "unsafe"}],
        )
