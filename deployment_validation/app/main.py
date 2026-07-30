from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .validator import (
    repair_deployment_bundle,
    validate_and_repair_deployment_bundle,
    validate_deployment_bundle,
)


app = FastAPI(title="InLumen Deployment Validation", version="0.2.0")


class ValidationRequest(BaseModel):
    bundle_path: str = Field(..., description="Path to a generated deployment bundle, dagster project, or Argo YAML.")
    targets: dict[str, bool] = Field(default_factory=dict, description="Deployment targets to validate.")
    validate_argo: bool | None = Field(default=None, description="Validate Argo workflow artifacts.")
    validate_dagster: bool | None = Field(default=None, description="Validate Dagster project artifacts.")
    materialize: bool = Field(default=True, description="Run Dagster asset materialization after definition load.")
    reinstall: bool = Field(default=False, description="Recreate the generated project validation venv before running.")
    skip_install: bool = Field(default=False, description="Reuse an existing validation venv and validate with PYTHONPATH=src.")
    argo_lint: bool = Field(default=False, description="Run optional argo lint when the argo CLI is available.")
    argo_dry_run: bool = Field(default=False, description="Run optional argo submit --dry-run when the argo CLI is available.")
    timeout_seconds: int = Field(default=900, ge=30, le=3600)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/validate")
def validate(request: ValidationRequest) -> dict:
    return validate_deployment_bundle(
        Path(request.bundle_path),
        targets=request.targets,
        validate_argo=request.validate_argo,
        validate_dagster=request.validate_dagster,
        materialize=request.materialize,
        reinstall=request.reinstall,
        skip_install=request.skip_install,
        argo_lint=request.argo_lint,
        argo_dry_run=request.argo_dry_run,
        timeout_seconds=request.timeout_seconds,
    )


@app.post("/repair")
def repair(request: ValidationRequest) -> dict:
    return repair_deployment_bundle(
        Path(request.bundle_path),
        targets=request.targets,
    )


@app.post("/validate-and-repair")
def validate_and_repair(request: ValidationRequest) -> dict:
    return validate_and_repair_deployment_bundle(
        Path(request.bundle_path),
        targets=request.targets,
        validate_argo=request.validate_argo,
        validate_dagster=request.validate_dagster,
        materialize=request.materialize,
        reinstall=request.reinstall,
        skip_install=request.skip_install,
        argo_lint=request.argo_lint,
        argo_dry_run=request.argo_dry_run,
        timeout_seconds=request.timeout_seconds,
    )
