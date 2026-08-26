import base64
import hashlib
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.deployment_validation import validate_deployment_bundle_files
from app.main import app


def artifact(path: str, content: bytes) -> dict:
    return {
        "path": path,
        "filename": path.rsplit("/", 1)[-1],
        "content": base64.b64encode(content).decode("ascii"),
        "content_encoding": "base64",
        "size_bytes": len(content),
        "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
    }


def minimal_bundle_files() -> list[dict]:
    return [
        artifact(
            "bundle-manifest.json",
            json.dumps(
                {
                    "schema_version": "inlumen.deployment-bundle@1",
                    "run_spec": "run-spec.json",
                    "targets": {"argo": False, "dagster": False},
                    "nodes": [],
                }
            ).encode(),
        ),
        artifact(
            "run-spec.json",
            json.dumps(
                {
                    "schema_version": "inlumen.run-spec@1",
                    "runtime": {"package_manager": "uv"},
                    "nodes": [],
                    "connections": [],
                }
            ).encode(),
        ),
        artifact(
            "inputs/input_manifest.json",
            b'{"schema_version":"inlumen.input-manifest@1","inputs":[]}',
        ),
        artifact("nodes/.gitkeep", b""),
        artifact("outputs/.gitkeep", b""),
    ]


def test_deployment_validation_endpoint_forwards_every_option() -> None:
    expected = {
        "ok": True,
        "validation_report": {"ok": True},
        "repair_report": None,
        "repaired_files": [],
        "run_outputs": [],
    }
    with patch(
        "app.main.validate_deployment_bundle_files",
        return_value=expected,
    ) as validate_bundle:
        response = TestClient(app).post(
            "/v1/validate/deployment-bundle",
            json={
                "files": minimal_bundle_files(),
                "targets": {"argo": True, "dagster": False},
                "mode": "repair",
                "validate_argo": True,
                "validate_dagster": False,
                "materialize": False,
                "reinstall": True,
                "skip_install": False,
                "argo_lint": True,
                "argo_dry_run": True,
            "timeout_seconds": 120,
            "execution_id": "run-1",
            },
        )

    assert response.status_code == 200
    assert response.json() == expected
    validate_bundle.assert_called_once_with(
        minimal_bundle_files(),
        targets={"argo": True, "dagster": False},
        mode="repair",
        validate_argo=True,
        validate_dagster=False,
        materialize=False,
        reinstall=True,
        skip_install=False,
        argo_lint=True,
            argo_dry_run=True,
            timeout_seconds=120,
        runtime_secrets={},
        execution_id="run-1",
    )


def test_uploaded_bundle_is_validated_without_a_shared_filesystem() -> None:
    result = validate_deployment_bundle_files(
        minimal_bundle_files(),
        targets={"argo": False, "dagster": False},
        mode="fast",
        validate_argo=False,
        validate_dagster=False,
        materialize=False,
    )

    assert result["ok"] is True
    assert result["validation_report"]["ok"] is True


def test_uploaded_bundle_rejects_tampered_binary_content() -> None:
    tampered = artifact("inputs/sample.pdf", b"original")
    tampered["content"] = base64.b64encode(b"tampered").decode("ascii")

    try:
        validate_deployment_bundle_files(
            [*minimal_bundle_files(), tampered],
            targets={"argo": False, "dagster": False},
            mode="fast",
            validate_argo=False,
            validate_dagster=False,
            materialize=False,
        )
    except ValueError as exc:
        assert "checksum mismatch" in str(exc).lower()
    else:
        raise AssertionError("Tampered deployment artifact was accepted")


def test_uploaded_bundle_rejects_path_traversal() -> None:
    unsafe = artifact("../escape.txt", b"unsafe")

    response = TestClient(app).post(
        "/v1/validate/deployment-bundle",
        json={
            "files": [unsafe],
            "targets": {"argo": False, "dagster": False},
            "mode": "fast",
        },
    )

    assert response.status_code == 422
    assert "Unsafe bundle file path" in response.json()["detail"]


def test_deployment_execution_cancellation_terminates_process_and_container() -> None:
    with (
        patch("app.main.cancel_deployment_execution") as cancel_process,
        patch("app.main.cancel_sandbox_run") as cancel_container,
    ):
        response = TestClient(app).delete(
            "/v1/validate/deployment-bundle/run-123"
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancellation_requested"
    cancel_process.assert_called_once_with("run-123")
    cancel_container.assert_called_once_with("run-123")
