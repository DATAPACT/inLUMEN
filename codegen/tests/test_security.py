from fastapi.testclient import TestClient

from app.main import app


def test_v1_endpoint_fails_closed_when_auth_is_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("CODEGEN_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("CODEGEN_SERVICE_API_KEY", raising=False)
    monkeypatch.delenv("CODEGEN_SERVICE_API_KEY_FILE", raising=False)

    response = TestClient(app).post("/v1/generate/node-script", json={})

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_v1_endpoint_requires_valid_bearer_token(monkeypatch) -> None:
    monkeypatch.delenv("CODEGEN_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("CODEGEN_SERVICE_API_KEY", "service-token")
    client = TestClient(app)

    missing = client.post("/v1/generate/node-script", json={})
    invalid = client.post(
        "/v1/generate/node-script",
        headers={"Authorization": "Bearer wrong-token"},
        json={},
    )
    valid = client.post(
        "/v1/generate/node-script",
        headers={"Authorization": "Bearer service-token"},
        json={},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 422  # Authenticated; request validation runs next.


def test_health_remains_public(monkeypatch) -> None:
    monkeypatch.delenv("CODEGEN_AUTH_DISABLED", raising=False)

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"


def test_readiness_fails_when_service_auth_is_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("CODEGEN_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("CODEGEN_SERVICE_API_KEY", raising=False)
    monkeypatch.delenv("CODEGEN_SERVICE_API_KEY_FILE", raising=False)

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "Service authentication is not configured"


def test_readiness_passes_when_service_auth_is_configured(monkeypatch) -> None:
    monkeypatch.delenv("CODEGEN_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("CODEGEN_SERVICE_API_KEY", "service-token")
    monkeypatch.delenv("CODEGEN_SERVICE_API_KEY_FILE", raising=False)

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_provider_api_key_is_never_serialized_in_codegen_response() -> None:
    response = TestClient(app).post(
        "/v1/generate/node-script",
        json={
            "context": {
                "target_node": {
                    "flow_id": "test",
                    "type": "action",
                }
            },
            "llm_config": {
                "provider": "openrouter",
                "model": "code-model",
                "base_url": "https://llm.invalid/v1",
            },
        },
    )

    assert response.status_code == 200
    assert "api_key" not in response.text
