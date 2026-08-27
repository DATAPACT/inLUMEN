from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal[
    "queued",
    "preparing",
    "running",
    "cancelling",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
]


class PipelineSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph: dict[str, Any]
    pipeline_id: str | None = None
    pipeline_version: str | None = None
    active_version_uid: str | None = None
    bundle_files: list[dict[str, Any]]
    bundle_manifest: dict[str, Any]


class CreatePipelineRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: PipelineSnapshotRequest
    idempotency_key: str | None = Field(default=None, max_length=200)
    runtime_secrets: dict[str, str] = Field(default_factory=dict, repr=False)


class PipelineSnapshotDescriptor(BaseModel):
    snapshot_id: str
    graph_sha256: str
    bundle_sha256: str
    pipeline_id: str | None = None
    pipeline_version: str | None = None
    active_version_uid: str | None = None
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)


class PipelineRunEvent(BaseModel):
    id: int = Field(ge=1)
    timestamp: str
    type: str
    status: RunStatus | None = None
    message: str | None = None
    node_id: str | None = None


class PipelineRunError(BaseModel):
    code: str
    message: str
    details: Any | None = None


class PipelineRunRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal["inlumen.pipeline-run@1"]
    run_id: str
    status: RunStatus
    engine: str
    execution_mode: str
    snapshot: PipelineSnapshotDescriptor
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested_at: str | None = None
    event_cursor: int = Field(default=0, ge=0)
    error: PipelineRunError | None = None
    result: dict[str, Any] | None = None


class PipelineRunListResponse(BaseModel):
    runs: list[PipelineRunRecord]


class PipelineRunEventsResponse(BaseModel):
    run_id: str
    events: list[PipelineRunEvent]
    next_cursor: int = Field(ge=0)


class RunnerCapabilities(BaseModel):
    background_runs: bool
    execution_available: bool
    adapter: str
    execution_mode: str
    max_outstanding_runs: int = Field(ge=1)
    outstanding_run_count: int = Field(ge=0)
    available_run_slots: int = Field(ge=0)
    summary_persistence: bool
    message: str | None = None
