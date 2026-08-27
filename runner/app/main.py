from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status

from .artifacts import PipelineArtifactStore
from .dagster_executor import CodegenDagsterExecutor
from .manager import (
    PipelineRunCapacityError,
    PipelineRunConflict,
    PipelineRunManager,
)
from .models import (
    CreatePipelineRunRequest,
    PipelineRunEventsResponse,
    PipelineRunListResponse,
    PipelineRunRecord,
    RunnerCapabilities,
)
from .run_summaries import Neo4jRunSummaryStore
from .security import configured_key, require_service_api_key
from .store import PipelineRunStore

DEFAULT_RUNNER_STATE = (
    Path(__file__).resolve().parents[1] / "state" / "pipeline-runs.sqlite3"
)
RUN_STORE = PipelineRunStore(os.getenv("RUNNER_JOB_DB_PATH", str(DEFAULT_RUNNER_STATE)))
RUN_ARTIFACT_STORE = PipelineArtifactStore(
    os.getenv("RUNNER_ARTIFACT_ROOT", str(DEFAULT_RUNNER_STATE.parent / "artifacts"))
)
RUN_SUMMARY_STORE = Neo4jRunSummaryStore()
RUN_MANAGER = PipelineRunManager(
    RUN_STORE,
    adapter=os.getenv("RUNNER_ADAPTER", "disabled").strip().lower(),
    executor=CodegenDagsterExecutor(),
    artifact_store=RUN_ARTIFACT_STORE,
    summary_store=RUN_SUMMARY_STORE,
)
SERVICE_AUTH = [Depends(require_service_api_key)]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await RUN_MANAGER.reconcile_interrupted()
    yield
    RUN_SUMMARY_STORE.close()


app = FastAPI(
    title="inLUMEN Pipeline Runner",
    version="0.1.0",
    description="Durable background pipeline-run lifecycle and execution adapters.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    if not configured_key():
        raise HTTPException(
            status_code=503, detail="Runner service authentication is not configured."
        )
    return {"status": "ready"}


@app.get(
    "/v1/pipeline-runs/capabilities",
    response_model=RunnerCapabilities,
    dependencies=SERVICE_AUTH,
)
def capabilities() -> dict:
    return RUN_MANAGER.capabilities()


@app.post(
    "/v1/pipeline-runs",
    response_model=PipelineRunRecord,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=SERVICE_AUTH,
)
async def create_pipeline_run(request: CreatePipelineRunRequest) -> dict:
    try:
        record, _created = await RUN_MANAGER.start(request)
        return record
    except PipelineRunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PipelineRunCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": str(exc),
                "code": "pipeline_run_capacity_full",
                "limit": exc.limit,
                "outstanding": exc.outstanding,
            },
            headers={"Retry-After": "5"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(
    "/v1/pipeline-runs",
    response_model=PipelineRunListResponse,
    dependencies=SERVICE_AUTH,
)
def list_pipeline_runs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    return {"runs": RUN_MANAGER.list(limit=limit)}


@app.delete(
    "/v1/pipeline-runs",
    dependencies=SERVICE_AUTH,
)
async def clear_pipeline_runs() -> dict[str, int]:
    return await RUN_MANAGER.clear_all()


@app.get(
    "/v1/pipeline-runs/{run_id}",
    response_model=PipelineRunRecord,
    dependencies=SERVICE_AUTH,
)
def get_pipeline_run(run_id: str) -> dict:
    record = RUN_MANAGER.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Pipeline run was not found.")
    return record


@app.get(
    "/v1/pipeline-runs/{run_id}/events",
    response_model=PipelineRunEventsResponse,
    dependencies=SERVICE_AUTH,
)
def get_pipeline_run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
) -> dict:
    payload = RUN_MANAGER.events(run_id, after=after)
    if payload is None:
        raise HTTPException(status_code=404, detail="Pipeline run was not found.")
    return payload


@app.delete(
    "/v1/pipeline-runs/{run_id}",
    response_model=PipelineRunRecord,
    dependencies=SERVICE_AUTH,
)
async def cancel_pipeline_run(run_id: str) -> dict:
    record = await RUN_MANAGER.cancel(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Pipeline run was not found.")
    return record


@app.get(
    "/v1/pipeline-runs/{run_id}/outputs/{output_path:path}",
    dependencies=SERVICE_AUTH,
)
def download_pipeline_run_output(run_id: str, output_path: str) -> Response:
    payload = RUN_MANAGER.output(run_id, output_path)
    if payload is None:
        raise HTTPException(status_code=404, detail="Pipeline output was not found.")
    body, content_type, filename = payload
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/v1/pipeline-runs/{run_id}/bundle",
    dependencies=SERVICE_AUTH,
)
def download_pipeline_run_bundle(run_id: str) -> Response:
    body = RUN_MANAGER.bundle_zip(run_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Pipeline run was not found.")
    return Response(
        content=body,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="inlumen-dagster-run-{run_id}.zip"'
            )
        },
    )
