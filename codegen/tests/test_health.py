import json
import os
import runpy
import time

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import PipelineGenerationRun, PipelineGenerationRunStep


def sample_payload() -> dict:
    return {
        "context": {
            "target_node": {
                "flow_id": "clean",
                "label": "Clean customer data",
                "description": "Normalize rows.",
                "type": "action",
            },
            "available_inputs": [
                {
                    "filename": "customers.csv",
                    "kind": "table",
                    "format": "csv",
                    "columns": ["name", "country"],
                }
            ],
            "expected_outputs": [
                {
                    "name": "cleaned_customers",
                    "kind": "json",
                    "format": "json",
                }
            ],
            "runtime_constraints": {
                "allowed_packages": ["pandas", "numpy"],
            },
        }
    }


def test_health() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_generate_node_script_is_deterministic_without_llm() -> None:
    client = TestClient(app)

    response = client.post("/v1/generate/node-script", json=sample_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["flow_id"] == "clean"
    artifact = body["generated_artifact"]
    assert artifact["validation_report"]["status"] == "valid"
    filenames = {item["filename"] for item in artifact["files"]}
    assert {
        "main.py",
        "requirements.txt",
        "node-manifest.json",
        "validation-report.json",
    } <= filenames
    assert not any(filename.startswith("Dockerfile.") for filename in filenames)


def test_pipeline_generation_run_reports_async_result() -> None:
    payload = {
        "context": {
            "graph": {
                "nodes": [
                    {
                        "flow_id": "ingest",
                        "label": "Data Ingestion",
                        "description": "Load input data.",
                        "type": "source",
                    },
                    {
                        "flow_id": "clean",
                        "label": "Data Cleaning",
                        "description": "Normalize input data.",
                        "type": "task",
                    },
                    {
                        "flow_id": "summary",
                        "label": "Summary",
                        "description": "Summarize ingested data.",
                        "type": "destination",
                    },
                ],
                "edges": [
                    {"source": "ingest", "target": "clean"},
                    {"source": "clean", "target": "summary"},
                ],
            },
            "runtime_constraints": {
                "allowed_packages": ["pandas", "numpy"],
            },
        },
        "options": {
            "validation_mode": "static",
            "allow_deterministic_fallback": True,
        },
    }

    with TestClient(app) as client:
        response = client.post("/v1/generate/pipeline-scripts/runs", json=payload)

        assert response.status_code == 202
        run_id = response.json()["run_id"]

        body = response.json()
        for _ in range(20):
            if body["status"] in {"valid", "invalid", "failed"}:
                break
            time.sleep(0.05)
            body = client.get(f"/v1/generate/pipeline-scripts/runs/{run_id}").json()

    assert body["status"] == "valid"
    assert body["generation_run"]["run_id"] == run_id
    assert body["result"]["nodes"][0]["flow_id"] == "ingest"


def test_pipeline_generation_runs_can_be_rediscovered() -> None:
    with TestClient(app) as client:
        started = client.post(
            "/v1/generate/pipeline-scripts/runs",
            json={
                "context": {
                    "graph": {
                        "nodes": [
                            {
                                "flow_id": "source",
                                "label": "Source",
                                "type": "input",
                            }
                        ],
                        "edges": [],
                    }
                },
                "options": {
                    "validation_mode": "static",
                    "allow_deterministic_fallback": True,
                },
            },
        )
        run_id = started.json()["run_id"]
        listed = client.get("/v1/generate/pipeline-scripts/runs?limit=10")

    assert listed.status_code == 200
    assert run_id in {job["run_id"] for job in listed.json()}


def test_unexpected_job_failure_is_terminal_in_nested_progress(monkeypatch) -> None:
    async def failing_generation(
        _request,
        *,
        run_id,
        progress_callback,
        **_kwargs,
    ):
        await progress_callback(
            PipelineGenerationRun(
                run_id=run_id,
                steps=[
                    PipelineGenerationRunStep(
                        flow_id="ingest",
                        status="running",
                        stage="dependency_installation",
                    ),
                    PipelineGenerationRunStep(
                        flow_id="summary",
                        status="running",
                        stage="dependency_installation",
                    ),
                ],
            )
        )
        raise RuntimeError("dependency resolver failed")

    monkeypatch.setattr(
        "app.main.generate_pipeline_script_bundles",
        failing_generation,
    )
    payload = {
        "context": {
            "graph": {
                "nodes": [
                    {"flow_id": "ingest", "label": "Input", "type": "input"},
                    {"flow_id": "summary", "label": "Summary", "type": "output"},
                ],
                "edges": [{"source": "ingest", "target": "summary"}],
            }
        }
    }

    with TestClient(app) as client:
        response = client.post("/v1/generate/pipeline-scripts/runs", json=payload)
        run_id = response.json()["run_id"]
        body = response.json()
        for _ in range(20):
            if body["status"] == "failed":
                break
            time.sleep(0.05)
            body = client.get(
                f"/v1/generate/pipeline-scripts/runs/{run_id}"
            ).json()

    assert body["status"] == "failed"
    assert body["generation_run"]["status"] == "failed"
    assert {step["status"] for step in body["generation_run"]["steps"]} == {
        "failed"
    }
    assert "dependency resolver failed" in body["generation_run"]["errors"]


def test_pipeline_generation_run_repairs_canonical_pipeline() -> None:
    payload = {
        "context": {
            "graph": {
                "nodes": [
                    {
                        "flow_id": "ingest",
                        "label": "Data Ingestion",
                        "description": "Load input data.",
                        "type": "input",
                    },
                    {
                        "flow_id": "summary",
                        "label": "Summary",
                        "description": "Summarize ingested data.",
                        "type": "output",
                    },
                ],
                "edges": [{"source": "ingest", "target": "summary"}],
            },
            "runtime_constraints": {
                "allowed_packages": ["pandas", "numpy"],
            },
        },
        "options": {
            "validation_mode": "static",
            "allow_deterministic_fallback": True,
        },
    }

    with TestClient(app) as client:
        response = client.post("/v1/generate/pipeline-scripts/runs", json=payload)
        source_run_id = response.json()["run_id"]
        source_body = response.json()
        for _ in range(20):
            if source_body["status"] in {"valid", "invalid", "failed"}:
                break
            time.sleep(0.05)
            source_body = client.get(
                f"/v1/generate/pipeline-scripts/runs/{source_run_id}"
            ).json()

        response = client.post(
            f"/v1/generate/pipeline-scripts/runs/{source_run_id}/resume",
            json={"flow_id": "summary", "repair_attempts": 4},
        )
        assert response.status_code == 202
        resume_body = response.json()
        resume_run_id = resume_body["run_id"]
        for _ in range(20):
            if resume_body["status"] in {"valid", "invalid", "failed"}:
                break
            time.sleep(0.05)
            resume_body = client.get(
                f"/v1/generate/pipeline-scripts/runs/{resume_run_id}"
            ).json()

    assert source_body["status"] == "valid"
    assert resume_body["status"] == "valid"
    assert resume_body["resumed_from_run_id"] == source_run_id
    assert resume_body["resume_from_flow_id"] == "summary"
    assert resume_body["generation_run"]["mode"] == "pipeline_first_single_script"
    steps = resume_body["generation_run"]["steps"]
    assert steps[0]["flow_id"] == "ingest"
    assert steps[0]["stage"] == "compiled_independent_bundle"
    assert steps[1]["flow_id"] == "summary"
    assert steps[1]["status"] == "valid"


def test_validate_node_script_rejects_bad_python() -> None:
    client = TestClient(app)
    payload = {
        **sample_payload(),
        "files": [
            {"filename": "main.py", "content": "def nope(:\n"},
            {"filename": "requirements.txt", "content": ""},
            {
                "filename": "Dockerfile.clean",
                "content": "FROM python:3.11-slim\nWORKDIR /app\nCMD []\n",
            },
            {"filename": "node-manifest.json", "content": '{"schema_version": 1}'},
        ],
    }

    response = client.post("/v1/validate/node-script", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "invalid"


def test_ast_security_allows_model_eval_but_rejects_builtin_eval() -> None:
    base_files = [
        {"filename": "requirements.txt", "content": "torch\n"},
        {
            "filename": "Dockerfile.clean",
            "content": (
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY requirements.txt main.py /app/\n"
                "CMD [\"python\", \"main.py\"]\n"
            ),
        },
        {"filename": "node-manifest.json", "content": '{"schema_version": 1}'},
    ]
    payload = {
        **sample_payload(),
        "context": {
            **sample_payload()["context"],
            "runtime_constraints": {"allowed_packages": ["torch"]},
        },
        "files": [
            {
                "filename": "main.py",
                "content": "import torch\nmodel = torch.nn.Linear(1, 1)\nmodel.eval()\n",
            },
            *base_files,
        ],
    }

    allowed = TestClient(app).post("/v1/validate/node-script", json=payload)
    payload["files"][0]["content"] = "value = eval('1 + 1')\n"
    rejected = TestClient(app).post("/v1/validate/node-script", json=payload)

    assert allowed.json()["status"] == "valid"
    assert rejected.json()["status"] == "invalid"
    assert "banned function: eval" in " ".join(rejected.json()["errors"])


def test_validate_node_script_rejects_missing_import_requirement() -> None:
    client = TestClient(app)
    payload = {
        **sample_payload(),
        "files": [
            {
                "filename": "main.py",
                "content": (
                    "import os\n"
                    "import pandas as pd\n\n"
                    "def main():\n"
                    "    print(os.getenv('INLUMEN_INPUT_MANIFEST'))\n"
                    "    print(os.getenv('INLUMEN_OUTPUT_DIR'))\n"
                    "    print(os.getenv('INLUMEN_OUTPUT_MANIFEST'))\n"
                    "    pd.DataFrame()\n"
                ),
            },
            {"filename": "requirements.txt", "content": ""},
            {
                "filename": "Dockerfile.clean",
                "content": (
                    "FROM python:3.11-slim\n"
                    "WORKDIR /app\n"
                    "COPY requirements.txt /app/requirements.txt\n"
                    "COPY main.py /app/main.py\n"
                    'CMD ["python", "/app/main.py"]\n'
                ),
            },
            {"filename": "node-manifest.json", "content": '{"schema_version": 1}'},
        ],
    }

    response = client.post("/v1/validate/node-script", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "invalid"
    assert (
        "main.py imports third-party package pandas but requirements.txt is missing pandas"
        in body["errors"]
    )


def test_deterministic_fallback_writes_required_metrics_schema(
    tmp_path, monkeypatch
) -> None:
    client = TestClient(app)
    payload = {
        "context": {
            "target_node": {
                "flow_id": "train",
                "label": "ModelTraining",
                "description": "Train predictive model on processed vitals.",
                "type": "action",
            },
            "available_inputs": [
                {
                    "filename": "preprocessing.csv",
                    "kind": "table",
                    "format": "csv",
                    "columns": ["patient_id", "heart_rate", "abnormal_condition"],
                    "required_columns": ["abnormal_condition"],
                    "sample": {
                        "rows": [
                            {
                                "patient_id": "p1",
                                "heart_rate": "80",
                                "abnormal_condition": "normal",
                            }
                        ]
                    },
                }
            ],
            "expected_outputs": [
                {
                    "name": "modeltraining_model",
                    "kind": "model",
                    "format": "pickle",
                    "filename": "modeltraining_model.pickle",
                },
                {
                    "name": "modeltraining_metrics",
                    "kind": "json",
                    "format": "json",
                    "filename": "modeltraining_metrics.json",
                    "schema": {
                        "type": "object",
                        "required": ["metrics", "target_column"],
                        "properties": {
                            "metrics": {"type": "object"},
                            "target_column": {
                                "type": "string",
                                "enum": ["abnormal_condition"],
                            },
                        },
                    },
                    "semantic_role": "model_metrics",
                },
            ],
        }
    }

    response = client.post("/v1/generate/node-script", json=payload)

    assert response.status_code == 200
    files = response.json()["generated_artifact"]["files"]
    main_py = next(item["content"] for item in files if item["filename"] == "main.py")
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "outputs"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "preprocessing.csv").write_text(
        "patient_id,heart_rate,abnormal_condition\np1,80,normal\n",
        encoding="utf-8",
    )
    input_manifest = input_dir / "input_manifest.json"
    input_manifest.write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "name": "preprocessing",
                        "filename": "preprocessing.csv",
                        "path": str(input_dir / "preprocessing.csv"),
                        "kind": "table",
                        "format": "csv",
                        "columns": ["patient_id", "heart_rate", "abnormal_condition"],
                        "required_columns": ["abnormal_condition"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    script_path = tmp_path / "main.py"
    script_path.write_text(main_py, encoding="utf-8")
    monkeypatch.setenv("INLUMEN_INPUT_MANIFEST", str(input_manifest))
    monkeypatch.setenv("INLUMEN_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv(
        "INLUMEN_OUTPUT_MANIFEST", str(output_dir / "output_manifest.json")
    )
    monkeypatch.setenv("INLUMEN_CONTEXT_PATH", str(tmp_path / "context.json"))
    (tmp_path / "context.json").write_text("{}\n", encoding="utf-8")

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        runpy.run_path(str(script_path), run_name="__main__")
    finally:
        os.chdir(cwd)

    metrics = json.loads((output_dir / "modeltraining_metrics.json").read_text())
    assert metrics["target_column"] == "abnormal_condition"
    assert isinstance(metrics["metrics"], dict)


def test_generate_pipeline_scripts_propagates_edge_contracts() -> None:
    client = TestClient(app)
    payload = {
        "context": {
            "pipeline": {"name": "Vitals"},
            "graph": {
                "nodes": [
                    {
                        "flow_id": "ingest",
                        "label": "Data Ingestion",
                        "description": "Load vital signs.",
                        "type": "input",
                        "files": [
                            {
                                "filename": "vitals.csv",
                                "kind": "table",
                                "format": "csv",
                                "columns": ["patient_id", "heart_rate"],
                            }
                        ],
                    },
                    {
                        "flow_id": "preprocess",
                        "label": "Preprocessing",
                        "description": "Clean vital signs.",
                        "type": "action",
                    },
                ],
                "edges": [{"source": "ingest", "target": "preprocess"}],
            },
        }
    }

    response = client.post("/v1/generate/pipeline-scripts", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert [item["flow_id"] for item in body["nodes"]] == ["ingest", "preprocess"]
    assert body["integration_validation"]["status"] == "valid"
    assert body["edges"][0]["source"] == "ingest"
    assert body["edges"][0]["target"] == "preprocess"
    assert body["edges"][0]["outputs"]
    assert body["edges"][0]["outputs"][0]["kind"] == "table"
    assert body["edges"][0]["outputs"][0]["format"] == "csv"
    run = body["generation_run"]
    assert run["status"] == "valid"
    assert run["mode"] == "pipeline_first_single_script"
    assert [(step["flow_id"], step["status"]) for step in run["steps"]] == [
        ("ingest", "valid"),
        ("preprocess", "valid"),
    ]


def test_pipeline_sample_validation_reports_not_run_without_docker() -> None:
    client = TestClient(app)
    payload = {
        "context": {
            "pipeline": {"name": "Vitals"},
            "graph": {
                "nodes": [
                    {
                        "flow_id": "ingest",
                        "label": "Ingestion",
                        "description": "Load vital signs.",
                        "type": "input",
                        "files": [
                            {
                                "filename": "vitals.csv",
                                "kind": "table",
                                "format": "csv",
                                "columns": ["patient_id", "heart_rate"],
                                "sample": {
                                    "rows": [{"patient_id": "p1", "heart_rate": "80"}]
                                },
                            }
                        ],
                    }
                ],
                "edges": [],
            },
        },
        "options": {"validation_mode": "pipeline_sample"},
    }

    response = client.post("/v1/generate/pipeline-scripts", json=payload)

    assert response.status_code == 200
    validation = response.json()["integration_validation"]
    assert validation["status"] == "valid"
    assert "whole_pipeline_sample_run" in validation["checks"]
    assert any(
        "Docker execution validation skipped" in item for item in validation["warnings"]
    )
