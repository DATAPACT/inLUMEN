import asyncio
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException

from .deployment_validation import (
    cancel_deployment_execution,
    deployment_execution_progress,
    finish_deployment_execution,
    prepare_deployment_execution,
    validate_deployment_bundle_files,
)
from .generator import generate_node_script_bundle, generate_pipeline_script_bundles
from .job_store import PipelineJobStore
from .llm import LLMGenerationError
from .sandbox import cancel_sandbox_run
from .schemas import (
    DeploymentBundleValidationRequest,
    GenerateNodeScriptRequest,
    GenerateNodeScriptResponse,
    GeneratePipelineScriptsRequest,
    GeneratePipelineScriptsResponse,
    GenerationUsage,
    LLMConfig,
    PipelineGenerationJobResponse,
    PipelineGenerationRun,
    ResumePipelineGenerationRunRequest,
    ValidateNodeScriptRequest,
    ValidationReport,
)
from .security import (
    SecurityHeadersMiddleware,
    require_service_api_key,
    service_auth_configuration_error,
)
from .validation import validate_generated_files

PIPELINE_JOB_STORE = PipelineJobStore(
    os.getenv("CODEGEN_JOB_DB_PATH", "state/codegen-jobs.sqlite3")
)
PIPELINE_GENERATION_JOBS: dict[str, dict[str, Any]] = PIPELINE_JOB_STORE.load_all()
PIPELINE_GENERATION_TASKS: dict[str, asyncio.Task[None]] = {}
PIPELINE_GENERATION_PURGED_RUN_IDS: set[str] = set()
PIPELINE_GENERATION_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = (
    OrderedDict()
)
PIPELINE_CACHE_SCHEMA_VERSION = "pipeline-first-v9-ai-runtime-artifacts"

app = FastAPI(
    title="InLumen Code Generation Service",
    version="0.1.0",
    description=(
        "Private inLUMEN service for code generation, sandbox validation, "
        "and deployment-bundle validation."
    ),
)
app.add_middleware(SecurityHeadersMiddleware)

SERVICE_AUTH = [Depends(require_service_api_key)]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def llm_config_with_request_key(
    config: LLMConfig | None,
    api_key: str | None,
) -> LLMConfig | None:
    if config is None:
        return None
    key = str(api_key or "").strip()
    return config.model_copy(update={"api_key": key}) if key else config


def require_generation_model(
    config: LLMConfig | None,
    *,
    allow_deterministic_fallback: bool,
) -> None:
    if allow_deterministic_fallback:
        return
    if config is None:
        raise HTTPException(
            status_code=422,
            detail="Code-generation model configuration is required.",
        )
    if not config.api_key.strip():
        raise HTTPException(
            status_code=422,
            detail="X-LLM-API-Key is required for code generation.",
        )


def update_pipeline_job(run_id: str, **updates: Any) -> None:
    if run_id in PIPELINE_GENERATION_PURGED_RUN_IDS:
        return
    now = utc_now_iso()
    job = PIPELINE_GENERATION_JOBS.setdefault(
        run_id,
        {
            "run_id": run_id,
            "status": "queued",
            "generation_run": None,
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    job.update(updates)
    job["updated_at"] = now
    PIPELINE_JOB_STORE.save(job)


def clear_pipeline_job_llm_key(run_id: str) -> None:
    """Drop the ephemeral provider credential as soon as a worker is terminal."""
    job = PIPELINE_GENERATION_JOBS.get(run_id)
    if job is None:
        return
    request = job.get("request")
    if not isinstance(request, GeneratePipelineScriptsRequest):
        return
    config = request.llm_config
    if config is None or not config.api_key:
        return
    safe_request = request.model_copy(
        update={"llm_config": config.model_copy(update={"api_key": ""})}
    )
    update_pipeline_job(run_id, request=safe_request)


def clear_pipeline_job_state(*, preserve_purged_run_ids: bool = False) -> None:
    """Clear job state for tests and explicit administrative resets."""
    PIPELINE_GENERATION_JOBS.clear()
    PIPELINE_JOB_STORE.clear()
    if not preserve_purged_run_ids:
        PIPELINE_GENERATION_PURGED_RUN_IDS.clear()


def pipeline_cache_key(request: GeneratePipelineScriptsRequest) -> str:
    canonical = json.dumps(
        {
            "cache_schema": PIPELINE_CACHE_SCHEMA_VERSION,
            "request": request.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cached_pipeline_response(
    cache_key: str,
    run_id: str,
) -> GeneratePipelineScriptsResponse | None:
    cached = PIPELINE_GENERATION_CACHE.get(cache_key)
    if cached is None:
        return None
    cached_at, payload = cached
    ttl_seconds = max(
        0.0, float(os.getenv("CODEGEN_PIPELINE_CACHE_TTL_SECONDS") or 3600)
    )
    if ttl_seconds == 0 or time.monotonic() - cached_at > ttl_seconds:
        PIPELINE_GENERATION_CACHE.pop(cache_key, None)
        return None

    PIPELINE_GENERATION_CACHE.move_to_end(cache_key)
    response = GeneratePipelineScriptsResponse.model_validate(payload)
    run = response.generation_run
    if run is None:
        return None
    run.run_id = run_id
    run.status = "valid"
    run.warnings = [*run.warnings, "Reused a matching validated pipeline result."]
    run.stage_timings_ms = {"validated_cache_lookup": 0}
    run.generation_usage = GenerationUsage(
        request_count=0,
        usage_reported_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost_usd=0.0,
    )
    for step in run.steps:
        step.status = "valid"
        step.stage = "validated_cache_hit"
        step.attempts = 0
    return response


def cache_pipeline_response(
    cache_key: str,
    response: GeneratePipelineScriptsResponse,
) -> None:
    if (
        response.integration_validation.status != "valid"
        or response.generation_run is None
        or response.generation_run.status != "valid"
    ):
        return
    PIPELINE_GENERATION_CACHE[cache_key] = (
        time.monotonic(),
        response.model_dump(mode="json"),
    )
    PIPELINE_GENERATION_CACHE.move_to_end(cache_key)
    max_entries = max(1, int(os.getenv("CODEGEN_PIPELINE_CACHE_MAX_ENTRIES") or 32))
    while len(PIPELINE_GENERATION_CACHE) > max_entries:
        PIPELINE_GENERATION_CACHE.popitem(last=False)


def mark_pipeline_job_cancelled(run_id: str) -> None:
    job = PIPELINE_GENERATION_JOBS.get(run_id)
    if job is None:
        return
    generation_run = job.get("generation_run")
    if isinstance(generation_run, dict):
        run = PipelineGenerationRun.model_validate(generation_run)
        run.status = "cancelled"
        run.current_stage = "cancelled"
        run.stage_started_at = utc_now_iso()
        if "Generation cancelled by the user." not in run.warnings:
            run.warnings.append("Generation cancelled by the user.")
        for step in run.steps:
            if step.status in {"pending", "running", "failed"}:
                step.status = "skipped"
                step.stage = "cancelled"
        generation_run = run.model_dump(mode="json")
    update_pipeline_job(
        run_id,
        status="cancelled",
        generation_run=generation_run,
        error="Generation cancelled by the user.",
    )


def mark_pipeline_job_failed(run_id: str, error: str) -> None:
    """Make outer and nested job state agree after an unexpected failure."""
    job = PIPELINE_GENERATION_JOBS.get(run_id)
    if job is None:
        return
    generation_run = job.get("generation_run")
    if isinstance(generation_run, dict):
        run = PipelineGenerationRun.model_validate(generation_run)
    else:
        run = PipelineGenerationRun(run_id=run_id)
    run.status = "failed"
    run.current_stage = "failed"
    run.stage_started_at = utc_now_iso()
    if error and error not in run.errors:
        run.errors.append(error)
    for step in run.steps:
        if step.status == "running":
            step.status = "failed"
            step.stage = (
                f"{step.stage}_failed"
                if step.stage and not step.stage.endswith("_failed")
                else "failed"
            )
        elif step.status == "pending":
            step.status = "skipped"
            step.stage = "blocked_by_generation_failure"
    update_pipeline_job(
        run_id,
        status="failed",
        generation_run=run.model_dump(mode="json"),
        error=error,
    )


def recover_interrupted_pipeline_jobs() -> None:
    """Turn process-local work left by a restart into resumable failed jobs."""
    for run_id, job in list(PIPELINE_GENERATION_JOBS.items()):
        if str(job.get("status") or "").lower() not in {"queued", "running"}:
            continue
        mark_pipeline_job_failed(
            run_id,
            "Code generation was interrupted by a service restart. Resume this run "
            "to retry it from its durable request snapshot.",
        )


recover_interrupted_pipeline_jobs()


def track_pipeline_task(run_id: str, task: asyncio.Task[None]) -> None:
    PIPELINE_GENERATION_TASKS[run_id] = task

    def discard_completed(completed: asyncio.Task[None]) -> None:
        if PIPELINE_GENERATION_TASKS.get(run_id) is completed:
            PIPELINE_GENERATION_TASKS.pop(run_id, None)

    task.add_done_callback(discard_completed)


async def run_pipeline_generation_job(
    run_id: str,
    request: GeneratePipelineScriptsRequest,
    *,
    resumed_from_run_id: str | None = None,
    resume_from_flow_id: str | None = None,
    seed_response: GeneratePipelineScriptsResponse | None = None,
) -> None:
    async def update_progress(run: PipelineGenerationRun) -> None:
        update_pipeline_job(
            run_id,
            status=run.status,
            generation_run=run.model_dump(mode="json"),
        )

    update_pipeline_job(run_id, status="running")
    cache_key = pipeline_cache_key(request)
    try:
        response = (
            cached_pipeline_response(cache_key, run_id)
            if resumed_from_run_id is None
            else None
        )
        if response is None:
            response = await generate_pipeline_script_bundles(
                request,
                run_id=run_id,
                progress_callback=update_progress,
                start_from_flow_id=resume_from_flow_id,
                seed_nodes=seed_response.nodes if seed_response is not None else None,
            )
            if resumed_from_run_id is None:
                cache_pipeline_response(cache_key, response)
    except asyncio.CancelledError:
        mark_pipeline_job_cancelled(run_id)
        clear_pipeline_job_llm_key(run_id)
        raise
    except Exception as exc:  # noqa: BLE001 - job state must capture all task failures
        if PIPELINE_GENERATION_JOBS.get(run_id, {}).get("status") == "cancelled":
            clear_pipeline_job_llm_key(run_id)
            return
        mark_pipeline_job_failed(run_id, str(exc))
        clear_pipeline_job_llm_key(run_id)
        return

    if PIPELINE_GENERATION_JOBS.get(run_id, {}).get("status") == "cancelled":
        clear_pipeline_job_llm_key(run_id)
        return
    generation_run = response.generation_run
    status = generation_run.status if generation_run is not None else "valid"
    update_pipeline_job(
        run_id,
        status=status,
        resumed_from_run_id=resumed_from_run_id,
        resume_from_flow_id=resume_from_flow_id,
        generation_run=generation_run.model_dump(mode="json")
        if generation_run is not None
        else None,
        result=response.model_dump(mode="json"),
        error=None,
    )
    clear_pipeline_job_llm_key(run_id)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    """Report whether protected generation endpoints can accept requests."""
    configuration_error = service_auth_configuration_error()
    if configuration_error:
        raise HTTPException(status_code=503, detail=configuration_error)
    return {"status": "ready"}


@app.post(
    "/v1/generate/node-script",
    response_model=GenerateNodeScriptResponse,
    dependencies=SERVICE_AUTH,
)
async def generate_node_script(
    request: GenerateNodeScriptRequest,
    x_llm_api_key: Annotated[str | None, Header(alias="X-LLM-API-Key")] = None,
) -> GenerateNodeScriptResponse:
    """Generate a validated script bundle for one pipeline node."""
    request = request.model_copy(
        update={
            "llm_config": llm_config_with_request_key(
                request.llm_config,
                x_llm_api_key,
            )
        }
    )
    require_generation_model(
        request.llm_config,
        allow_deterministic_fallback=request.options.allow_deterministic_fallback,
    )
    try:
        return await generate_node_script_bundle(request)
    except LLMGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/v1/generate/pipeline-scripts",
    response_model=GeneratePipelineScriptsResponse,
    dependencies=SERVICE_AUTH,
)
async def generate_pipeline_scripts(
    request: GeneratePipelineScriptsRequest,
    x_llm_api_key: Annotated[str | None, Header(alias="X-LLM-API-Key")] = None,
) -> GeneratePipelineScriptsResponse:
    """Generate validated script bundles for executable nodes in graph order."""
    request = request.model_copy(
        update={
            "llm_config": llm_config_with_request_key(
                request.llm_config,
                x_llm_api_key,
            )
        }
    )
    require_generation_model(
        request.llm_config,
        allow_deterministic_fallback=request.options.allow_deterministic_fallback,
    )
    try:
        return await generate_pipeline_script_bundles(request)
    except (LLMGenerationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/v1/generate/pipeline-scripts/runs",
    response_model=PipelineGenerationJobResponse,
    status_code=202,
    dependencies=SERVICE_AUTH,
)
async def start_pipeline_generation_run(
    request: GeneratePipelineScriptsRequest,
    x_llm_api_key: Annotated[str | None, Header(alias="X-LLM-API-Key")] = None,
) -> PipelineGenerationJobResponse:
    """Start an async pipeline script generation job with per-node progress."""
    request = request.model_copy(
        update={
            "llm_config": llm_config_with_request_key(
                request.llm_config,
                x_llm_api_key,
            )
        }
    )
    require_generation_model(
        request.llm_config,
        allow_deterministic_fallback=request.options.allow_deterministic_fallback,
    )
    run_id = uuid.uuid4().hex
    update_pipeline_job(
        run_id,
        status="queued",
        request=request,
    )
    task = asyncio.create_task(run_pipeline_generation_job(run_id, request))
    track_pipeline_task(run_id, task)
    return PipelineGenerationJobResponse.model_validate(
        PIPELINE_GENERATION_JOBS[run_id]
    )


@app.get(
    "/v1/generate/pipeline-scripts/runs",
    response_model=list[PipelineGenerationJobResponse],
    dependencies=SERVICE_AUTH,
)
def list_pipeline_generation_runs(
    limit: int = 20,
    status: str | None = None,
    include_result: bool = False,
) -> list[PipelineGenerationJobResponse]:
    """List recent durable jobs so clients can reattach after navigating away."""
    statuses = (
        {item.strip().lower() for item in status.split(",") if item.strip()}
        if status
        else None
    )
    jobs = PIPELINE_JOB_STORE.list(limit=min(max(limit, 1), 100), statuses=statuses)
    for job in jobs:
        run_id = str(job.get("run_id") or "")
        if run_id:
            PIPELINE_GENERATION_JOBS[run_id] = job
    if not include_result:
        jobs = [
            {key: value for key, value in job.items() if key != "result"}
            for job in jobs
        ]
    return [PipelineGenerationJobResponse.model_validate(job) for job in jobs]


@app.delete(
    "/v1/generate/pipeline-scripts/runs",
    dependencies=SERVICE_AUTH,
)
async def clear_pipeline_generation_runs() -> dict[str, int | str]:
    """Cancel active work and remove all durable generation history."""
    run_ids = list(PIPELINE_GENERATION_JOBS)
    active_run_ids = [
        run_id
        for run_id in run_ids
        if str(PIPELINE_GENERATION_JOBS[run_id].get("status") or "").lower()
        not in {"valid", "invalid", "failed", "cancelled"}
    ]
    for run_id in active_run_ids:
        mark_pipeline_job_cancelled(run_id)
    # Prevent callbacks from sandbox worker threads from recreating records
    # after their owning workspace has been cleared.
    PIPELINE_GENERATION_PURGED_RUN_IDS.update(run_ids)
    if active_run_ids:
        await asyncio.gather(
            *(
                asyncio.to_thread(cancel_sandbox_run, run_id)
                for run_id in active_run_ids
            ),
            return_exceptions=True,
        )
    tasks = [
        task
        for run_id, task in list(PIPELINE_GENERATION_TASKS.items())
        if run_id in active_run_ids
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    PIPELINE_GENERATION_TASKS.clear()
    clear_pipeline_job_state(preserve_purged_run_ids=True)
    PIPELINE_GENERATION_CACHE.clear()
    return {"status": "cleared", "deleted_count": len(run_ids)}


@app.post(
    "/v1/generate/pipeline-scripts/runs/{run_id}/resume",
    response_model=PipelineGenerationJobResponse,
    status_code=202,
    dependencies=SERVICE_AUTH,
)
async def resume_pipeline_generation_run(
    run_id: str,
    request: ResumePipelineGenerationRunRequest,
    x_llm_api_key: Annotated[str | None, Header(alias="X-LLM-API-Key")] = None,
) -> PipelineGenerationJobResponse:
    """Start a new async run from a failed node, reusing upstream artifacts."""
    source_job = PIPELINE_GENERATION_JOBS.get(run_id)
    if source_job is None:
        raise HTTPException(status_code=404, detail="pipeline generation run not found")
    original_request = source_job.get("request")
    if not isinstance(original_request, GeneratePipelineScriptsRequest):
        raise HTTPException(
            status_code=409,
            detail="pipeline generation run cannot be resumed; original request is unavailable",
        )
    result = source_job.get("result")
    source_response = (
        GeneratePipelineScriptsResponse.model_validate(result)
        if isinstance(result, dict)
        else None
    )
    resume_from_flow_id = request.flow_id or failed_flow_id_from_job(source_job)
    if not resume_from_flow_id and source_response is not None:
        raise HTTPException(
            status_code=422,
            detail="No failed node was found; provide flow_id to resume from a specific node",
        )
    if source_response is None:
        # No validated checkpoint exists after an infrastructure/shared-stage
        # exception. Retry the complete pipeline from the retained safe request.
        resume_from_flow_id = None

    options_update: dict[str, Any] = {}
    if request.repair_attempts is not None:
        options_update["repair_attempts"] = max(0, int(request.repair_attempts))
    else:
        options_update["repair_attempts"] = max(
            4,
            int(original_request.options.repair_attempts),
        )
    if request.user_instruction.strip():
        options_update["user_instruction"] = request.user_instruction.strip()
    if (
        source_response is not None
        and resume_from_flow_id
        and str(source_job.get("status") or "").lower() in {"invalid", "failed"}
    ):
        # The first attempt remains pipeline-first. A targeted retry can safely
        # reuse valid upstream bundles and regenerate only the failed tail.
        options_update["generation_strategy"] = "node_first"
    resume_llm_config = llm_config_with_request_key(
        request.llm_config or original_request.llm_config,
        x_llm_api_key,
    )
    require_generation_model(
        resume_llm_config,
        allow_deterministic_fallback=original_request.options.allow_deterministic_fallback,
    )
    resume_request = original_request.model_copy(
        deep=True,
        update={
            "options": original_request.options.model_copy(update=options_update),
            "llm_config": resume_llm_config,
        },
    )
    new_run_id = uuid.uuid4().hex
    update_pipeline_job(
        new_run_id,
        status="queued",
        request=resume_request,
        resumed_from_run_id=run_id,
        resume_from_flow_id=resume_from_flow_id,
    )
    task = asyncio.create_task(
        run_pipeline_generation_job(
            new_run_id,
            resume_request,
            resumed_from_run_id=run_id,
            resume_from_flow_id=resume_from_flow_id,
            seed_response=source_response,
        )
    )
    track_pipeline_task(new_run_id, task)
    return PipelineGenerationJobResponse.model_validate(
        PIPELINE_GENERATION_JOBS[new_run_id]
    )


@app.get(
    "/v1/generate/pipeline-scripts/runs/{run_id}",
    response_model=PipelineGenerationJobResponse,
    dependencies=SERVICE_AUTH,
)
def get_pipeline_generation_run(run_id: str) -> PipelineGenerationJobResponse:
    """Fetch async pipeline script generation progress or final result."""
    job = PIPELINE_GENERATION_JOBS.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="pipeline generation run not found")
    return PipelineGenerationJobResponse.model_validate(job)


@app.delete(
    "/v1/generate/pipeline-scripts/runs/{run_id}",
    response_model=PipelineGenerationJobResponse,
    dependencies=SERVICE_AUTH,
)
async def cancel_pipeline_generation_run(
    run_id: str,
) -> PipelineGenerationJobResponse:
    """Cancel an active async pipeline generation job."""
    job = PIPELINE_GENERATION_JOBS.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="pipeline generation run not found")
    terminal_status = job.get("status")
    if terminal_status in {"valid", "invalid", "cancelled"}:
        return PipelineGenerationJobResponse.model_validate(job)

    task = PIPELINE_GENERATION_TASKS.get(run_id)
    # Cancellation is authoritative before sandbox teardown begins. The worker
    # thread may observe its container being removed and raise while teardown
    # is still in progress; that race must not overwrite the user-requested
    # terminal state with "failed".
    mark_pipeline_job_cancelled(run_id)
    await asyncio.to_thread(cancel_sandbox_run, run_id)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    mark_pipeline_job_cancelled(run_id)
    return PipelineGenerationJobResponse.model_validate(
        PIPELINE_GENERATION_JOBS[run_id]
    )


def failed_flow_id_from_job(job: dict[str, Any]) -> str:
    generation_run = job.get("generation_run")
    if not isinstance(generation_run, dict):
        return ""
    steps = generation_run.get("steps")
    if not isinstance(steps, list):
        return ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("status") or "").lower() in {"invalid", "failed"}:
            return str(step.get("flow_id") or "").strip()
    return ""


@app.post(
    "/v1/validate/node-script",
    response_model=ValidationReport,
    dependencies=SERVICE_AUTH,
)
def validate_node_script(request: ValidateNodeScriptRequest) -> ValidationReport:
    """Validate a generated or uploaded script bundle against the runtime contract."""
    return validate_generated_files(
        files=request.files,
        runtime_constraints=request.context.runtime_constraints,
    )


@app.post(
    "/v1/validate/deployment-bundle",
    dependencies=SERVICE_AUTH,
)
async def validate_deployment_bundle_endpoint(
    request: DeploymentBundleValidationRequest,
) -> dict[str, Any]:
    """Validate deployment artifacts inside the private codegen service."""
    try:
        if request.execution_id:
            prepare_deployment_execution(request.execution_id)
        result = await asyncio.to_thread(
            validate_deployment_bundle_files,
            request.files,
            targets=request.targets,
            mode=request.mode,
            validate_argo=request.validate_argo,
            validate_dagster=request.validate_dagster,
            materialize=request.materialize,
            reinstall=request.reinstall,
            skip_install=request.skip_install,
            argo_lint=request.argo_lint,
            argo_dry_run=request.argo_dry_run,
            timeout_seconds=request.timeout_seconds,
            runtime_secrets=request.runtime_secrets,
            execution_id=request.execution_id,
        )
        finish_deployment_execution(
            request.execution_id,
            succeeded=bool(result.get("ok")),
        )
        return result
    except (TypeError, ValueError) as exc:
        finish_deployment_execution(request.execution_id, succeeded=False)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        finish_deployment_execution(request.execution_id, succeeded=False)
        raise


@app.get(
    "/v1/validate/deployment-bundle/{execution_id}/progress",
    dependencies=SERVICE_AUTH,
)
def get_deployment_bundle_execution_progress(execution_id: str) -> dict[str, Any]:
    """Observe private, bounded progress for an active background execution."""
    return deployment_execution_progress(execution_id)


@app.delete(
    "/v1/validate/deployment-bundle/{execution_id}",
    dependencies=SERVICE_AUTH,
)
async def cancel_deployment_bundle_execution(execution_id: str) -> dict[str, Any]:
    """Cancel installation or Dagster materialization for a background run."""
    await asyncio.gather(
        asyncio.to_thread(cancel_deployment_execution, execution_id),
        asyncio.to_thread(cancel_sandbox_run, execution_id),
    )
    return {"execution_id": execution_id, "status": "cancellation_requested"}
