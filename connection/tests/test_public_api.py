import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth_middleware import AuthValidationError  # noqa: E402
from deployment_artifacts import build_dockerfile_artifacts  # noqa: E402
from public_api import create_public_api_blueprint, generate_signed_url  # noqa: E402


API_TOKEN = "test-public-token"


def _auth_headers(token: str = API_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _sample_graph() -> dict:
    return {
        "updated_at": "2026-01-02T03:04:05.123456789Z",
        "pipeline": {
            "uid": "pipeline-123",
            "name": "Remote patient monitoring",
            "label": "RPM",
            "description": "Monitoring flow",
            "version": "Main",
            "active_version_uid": "main",
            "active_version_name": "Main",
            "status": "design",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T03:04:05.123456789Z",
            "step_count": 1,
        },
        "nodes": [
            {
                "id": "1",
                "data": {
                    "label": "Retrieve",
                    "type": "input",
                    "file_buckets": [
                        {
                            "filename": "retrieve.sh",
                            "bucket": "files-step-id-1",
                        }
                    ],
                },
            }
        ],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def _sample_versions(include_graph: bool = False) -> list[dict]:
    version = {
        "uid": "main",
        "name": "Main",
        "version": "Main",
        "is_main": True,
        "node_count": 1,
        "edge_count": 0,
        "file_count": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T03:04:05.123456789Z",
    }
    if include_graph:
        version["graph"] = _sample_graph()
    return [version]


class PublicApiTest(unittest.TestCase):
    def setUp(self):
        self.previous_token = os.environ.get("API_AUTH_TOKEN")
        self.previous_auth_enabled = os.environ.get("AUTH_ENABLED")
        self.previous_keycloak_jwks_url = os.environ.get("KEYCLOAK_JWKS_URL")
        os.environ["API_AUTH_TOKEN"] = API_TOKEN
        os.environ["AUTH_ENABLED"] = "false"
        os.environ.pop("KEYCLOAK_JWKS_URL", None)
        app = Flask(__name__)
        app.register_blueprint(create_public_api_blueprint("http://neo4j.example"))
        self.client = app.test_client()

    def tearDown(self):
        if self.previous_token is None:
            os.environ.pop("API_AUTH_TOKEN", None)
        else:
            os.environ["API_AUTH_TOKEN"] = self.previous_token
        if self.previous_auth_enabled is None:
            os.environ.pop("AUTH_ENABLED", None)
        else:
            os.environ["AUTH_ENABLED"] = self.previous_auth_enabled
        if self.previous_keycloak_jwks_url is None:
            os.environ.pop("KEYCLOAK_JWKS_URL", None)
        else:
            os.environ["KEYCLOAK_JWKS_URL"] = self.previous_keycloak_jwks_url

    def test_health_is_public(self):
        response = self.client.get("/health")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.get_json())

    def test_swagger_ui_is_available(self):
        response = self.client.get("/docs")

        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        self.assertIn("SwaggerUIBundle", body)
        self.assertIn("API_AUTH_TOKEN", body)
        self.assertIn("/openapi.json", body)

    def test_openapi_requires_auth_and_contains_bearer_security(self):
        unauthenticated = self.client.get("/openapi.json")
        self.assertEqual(401, unauthenticated.status_code)

        response = self.client.get("/openapi.json", headers=_auth_headers())

        self.assertEqual(200, response.status_code)
        schema = response.get_json()
        self.assertEqual("3.0.3", schema["openapi"])
        self.assertEqual(
            "bearer",
            schema["components"]["securitySchemes"]["bearerAuth"]["scheme"],
        )
        self.assertIn("/api/v1/pipelines/{pipeline_id}/artifacts/dockerfiles", schema["paths"])
        self.assertIn("/api/v1/pipelines/{pipeline_id}/artifacts/argo-workflow.yaml", schema["paths"])
        self.assertFalse(any("sim-pipe" in path for path in schema["paths"]))
        self.assertIn("/openapi.json", schema["paths"])
        self.assertEqual(["Health"], schema["paths"]["/health"]["get"]["tags"])

    def test_protected_endpoints_reject_missing_or_invalid_token_without_echoing_secret(self):
        missing = self.client.get("/api/v1/pipelines")
        invalid = self.client.get("/api/v1/pipelines", headers=_auth_headers("wrong-token"))

        self.assertEqual(401, missing.status_code)
        self.assertEqual(403, invalid.status_code)
        self.assertNotIn("wrong-token", invalid.get_data(as_text=True))

    def test_ready_uses_keycloak_configuration_when_auth_is_enabled(self):
        os.environ["AUTH_ENABLED"] = "true"
        os.environ.pop("API_AUTH_TOKEN", None)
        os.environ["KEYCLOAK_JWKS_URL"] = "http://keycloak.example/realms/inlumen/protocol/openid-connect/certs"

        response = self.client.get("/ready")

        self.assertEqual(200, response.status_code)
        self.assertEqual("keycloak", response.get_json()["checks"]["auth_mode"])
        self.assertEqual("configured", response.get_json()["checks"]["keycloak_jwks_url"])

    @patch("public_api.validate_keycloak_bearer_token")
    def test_keycloak_mode_accepts_valid_jwt_without_static_api_token(self, validate_token_mock):
        os.environ["AUTH_ENABLED"] = "true"
        os.environ.pop("API_AUTH_TOKEN", None)
        validate_token_mock.return_value = ({"sub": "user-123"}, None)

        with patch("public_api.fetch_pipeline_graph") as fetch_pipeline_graph_mock:
            fetch_pipeline_graph_mock.side_effect = _async_return(_sample_graph())
            response = self.client.get("/api/v1/pipelines", headers=_auth_headers("keycloak-jwt"))

        self.assertEqual(200, response.status_code)
        validate_token_mock.assert_called_once()

    @patch("public_api.validate_keycloak_bearer_token")
    def test_keycloak_mode_rejects_invalid_jwt_without_echoing_secret(self, validate_token_mock):
        os.environ["AUTH_ENABLED"] = "true"
        os.environ.pop("API_AUTH_TOKEN", None)
        validate_token_mock.return_value = (
            None,
            AuthValidationError(401, "Unauthorized", "Signature verification failed"),
        )

        response = self.client.get("/api/v1/pipelines", headers=_auth_headers("bad-keycloak-jwt"))

        self.assertEqual(401, response.status_code)
        self.assertEqual("unauthorized", response.get_json()["error"]["code"])
        self.assertNotIn("bad-keycloak-jwt", response.get_data(as_text=True))

    @patch("public_api.fetch_pipeline_graph")
    def test_authenticated_pipeline_listing(self, fetch_pipeline_graph_mock):
        fetch_pipeline_graph_mock.side_effect = _async_return(_sample_graph())

        response = self.client.get("/api/v1/pipelines", headers=_auth_headers())

        self.assertEqual(200, response.status_code)
        fetch_pipeline_graph_mock.assert_called_once_with(
            "http://neo4j.example",
            authorization=f"Bearer {API_TOKEN}",
        )
        pipelines = response.get_json()["pipelines"]
        self.assertEqual("pipeline-123", pipelines[0]["id"])
        self.assertEqual(1, pipelines[0]["node_count"])

    @patch("public_api.fetch_pipeline_graph")
    def test_fetch_pipeline_returns_404_for_unknown_pipeline(self, fetch_pipeline_graph_mock):
        fetch_pipeline_graph_mock.side_effect = _async_return(_sample_graph())

        found = self.client.get("/api/v1/pipelines/pipeline-123", headers=_auth_headers())
        missing = self.client.get("/api/v1/pipelines/missing-pipeline", headers=_auth_headers())

        self.assertEqual(200, found.status_code)
        self.assertEqual(404, missing.status_code)

    @patch("public_api.fetch_pipeline_versions")
    @patch("public_api.fetch_pipeline_graph")
    def test_workflow_versions_are_derived_from_modification_dates(
        self,
        fetch_pipeline_graph_mock,
        fetch_pipeline_versions_mock,
    ):
        fetch_pipeline_graph_mock.side_effect = _async_return(_sample_graph())
        fetch_pipeline_versions_mock.side_effect = _async_return(_sample_versions(include_graph=False))

        response = self.client.get("/api/v1/workflows/versions", headers=_auth_headers())

        self.assertEqual(200, response.status_code)
        version = response.get_json()["versions"][0]
        self.assertEqual("pipeline-123", version["pipeline_id"])
        self.assertEqual("v20260102T030405Z", version["version"])

    @patch("public_api.generate_signed_url")
    @patch("public_api.fetch_pipeline_versions")
    @patch("public_api.fetch_pipeline_graph")
    def test_workflows_endpoint_returns_pipeline_ids_versions_and_signed_urls(
        self,
        fetch_pipeline_graph_mock,
        fetch_pipeline_versions_mock,
        generate_signed_url_mock,
    ):
        fetch_pipeline_graph_mock.side_effect = _async_return(_sample_graph())

        async def _versions(_base_url, include_graph=False):
            return _sample_versions(include_graph=include_graph)

        fetch_pipeline_versions_mock.side_effect = _versions
        generate_signed_url_mock.return_value = "https://minio.example/signed/retrieve.sh"

        response = self.client.get(
            "/api/v1/workflows?include_download_urls=true",
            headers=_auth_headers(),
        )

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        workflow = payload["workflows"][0]
        self.assertEqual("pipeline-123", workflow["pipeline_id"])
        self.assertEqual(["pipeline-123"], workflow["pipeline_ids"])
        self.assertEqual("v20260102T030405Z", workflow["version"])
        self.assertEqual("https://minio.example/signed/retrieve.sh", workflow["download_url"])
        self.assertEqual("retrieve.sh", workflow["access_urls"][0]["name"])

    @patch("public_api._build_dockerfile_artifacts_or_error")
    @patch("public_api.fetch_pipeline_graph")
    def test_pipeline_artifact_endpoints_return_dockerfiles_and_yaml(
        self,
        fetch_pipeline_graph_mock,
        build_dockerfile_artifacts_mock,
    ):
        fetch_pipeline_graph_mock.side_effect = _async_return(_sample_graph())
        build_dockerfile_artifacts_mock.side_effect = build_dockerfile_artifacts

        dockerfiles = self.client.get(
            "/api/v1/pipelines/pipeline-123/artifacts/dockerfiles",
            headers=_auth_headers(),
        )
        yaml_response = self.client.get(
            "/api/v1/pipelines/pipeline-123/artifacts/argo-workflow.yaml",
            headers=_auth_headers(),
        )

        self.assertEqual(200, dockerfiles.status_code)
        dockerfile_payload = dockerfiles.get_json()
        self.assertEqual("pipeline-123", dockerfile_payload["pipeline_id"])
        self.assertEqual("Dockerfile.1", dockerfile_payload["dockerfiles"][0]["dockerfile_filename"])
        self.assertIn("FROM", dockerfile_payload["dockerfiles"][0]["content"])
        self.assertTrue(dockerfile_payload["guardrails"]["valid"])

        self.assertEqual(200, yaml_response.status_code)
        self.assertIn("application/x-yaml", yaml_response.headers["Content-Type"])
        yaml_text = yaml_response.get_data(as_text=True)
        self.assertIn('apiVersion: "argoproj.io/v1alpha1"', yaml_text)
        self.assertIn('kind: "Workflow"', yaml_text)

    @patch("public_api._build_dockerfile_artifacts_or_error")
    @patch("public_api.fetch_pipeline_versions")
    @patch("public_api.fetch_pipeline_graph")
    def test_version_artifact_endpoint_uses_requested_version_graph(
        self,
        fetch_pipeline_graph_mock,
        fetch_pipeline_versions_mock,
        build_dockerfile_artifacts_mock,
    ):
        version_graph = _sample_graph()
        version_graph["nodes"][0]["id"] = "version-step"
        version_graph["nodes"][0]["data"]["file_buckets"][0]["bucket"] = "files-step-id-version-step"
        fetch_pipeline_graph_mock.side_effect = _async_return(_sample_graph())
        build_dockerfile_artifacts_mock.side_effect = build_dockerfile_artifacts
        fetch_pipeline_versions_mock.side_effect = _async_return([
            {
                **_sample_versions(include_graph=False)[0],
                "uid": "version-1",
                "name": "Version 1",
                "graph": version_graph,
            }
        ])

        response = self.client.get(
            "/api/v1/pipelines/pipeline-123/versions/version-1/artifacts/dockerfiles",
            headers=_auth_headers(),
        )

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("version-1", payload["version_id"])
        self.assertEqual("Dockerfile.version-step", payload["dockerfiles"][0]["dockerfile_filename"])

    @patch("public_api.get_minio_client")
    def test_signed_url_generation_uses_temporary_minio_url(self, get_minio_client_mock):
        client = Mock()
        client.presigned_get_object.return_value = "https://minio.example/temp"
        get_minio_client_mock.return_value = client

        url = generate_signed_url("files-step-id-1", "retrieve.sh", expires_seconds=900)

        self.assertEqual("https://minio.example/temp", url)
        client.presigned_get_object.assert_called_once_with(
            "files-step-id-1",
            "retrieve.sh",
            expires=timedelta(seconds=900),
        )

    def test_request_validation_rejects_invalid_pipeline_create_body(self):
        missing_name = self.client.post(
            "/api/v1/pipelines",
            json={},
            headers=_auth_headers(),
        )
        not_json = self.client.post(
            "/api/v1/pipelines",
            data="not-json",
            headers=_auth_headers(),
            content_type="text/plain",
        )

        self.assertEqual(422, missing_name.status_code)
        self.assertEqual(400, not_json.status_code)


if __name__ == "__main__":
    unittest.main()
