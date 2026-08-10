from app.job_store import PipelineJobStore
from app.schemas import GeneratePipelineScriptsRequest


def request_payload() -> dict:
    return {
        "context": {
            "graph": {
                "nodes": [
                    {
                        "flow_id": "ingest",
                        "label": "Ingest",
                        "type": "input",
                    }
                ],
                "edges": [],
            }
        },
        "options": {"validation_mode": "static"},
    }


def test_pipeline_job_survives_store_reopen(tmp_path) -> None:
    path = tmp_path / "codegen-jobs.sqlite3"
    first = PipelineJobStore(str(path))
    first.save(
        {
            "run_id": "run-1",
            "status": "running",
            "request": GeneratePipelineScriptsRequest.model_validate(
                request_payload()
            ),
            "created_at": "2026-08-10T10:00:00Z",
            "updated_at": "2026-08-10T10:00:01Z",
        }
    )

    second = PipelineJobStore(str(path))
    restored = second.get("run-1")

    assert restored is not None
    assert restored["status"] == "running"
    assert isinstance(restored["request"], GeneratePipelineScriptsRequest)
    assert restored["request"].context.graph.nodes[0].flow_id == "ingest"


def test_pipeline_job_list_can_filter_terminal_state() -> None:
    store = PipelineJobStore(":memory:")
    for run_id, status in (("running", "running"), ("done", "valid")):
        store.save(
            {
                "run_id": run_id,
                "status": status,
                "request": GeneratePipelineScriptsRequest.model_validate(
                    request_payload()
                ),
                "created_at": "2026-08-10T10:00:00Z",
                "updated_at": "2026-08-10T10:00:01Z",
            }
        )

    jobs = store.list(statuses={"running"})

    assert [job["run_id"] for job in jobs] == ["running"]


def test_historical_request_schema_does_not_block_store_loading() -> None:
    store = PipelineJobStore(":memory:")
    legacy_request = request_payload()
    legacy_request["llm_config"] = {
        "model": "legacy-code-model",
        "base_url": "https://llm.example/v1",
    }
    store.save(
        {
            "run_id": "legacy",
            "status": "valid",
            "request": legacy_request,
            "created_at": "2026-08-10T10:00:00Z",
            "updated_at": "2026-08-10T10:00:01Z",
        }
    )

    restored = store.get("legacy")

    assert restored is not None
    assert restored["status"] == "valid"
    assert restored["request"] is None


def test_provider_key_is_not_persisted_with_job_request() -> None:
    store = PipelineJobStore(":memory:")
    payload = request_payload()
    payload["llm_config"] = {
        "provider": "openrouter",
        "model": "code-model",
        "base_url": "https://llm.example/v1",
        "api_key": "provider-secret",
    }
    request = GeneratePipelineScriptsRequest.model_validate(payload)
    store.save(
        {
            "run_id": "secure",
            "status": "running",
            "request": request,
            "created_at": "2026-08-10T10:00:00Z",
            "updated_at": "2026-08-10T10:00:01Z",
        }
    )

    restored = store.get("secure")

    assert restored is not None
    assert restored["request"].llm_config.api_key == ""
