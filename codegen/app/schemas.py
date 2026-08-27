import os
from typing import Any, Literal

from pydantic import BaseModel, Field

NodeKind = Literal[
    "input",
    "action",
    "output",
    "config",
    "storage",
    "api",
    "custom",
    "source",
    "task",
    "destination",
    "flow",
    "subpipeline",
]
ArtifactKind = Literal[
    "table",
    "json",
    "text",
    "image",
    "audio",
    "video",
    "document",
    "model",
    "directory",
    "binary",
]
ValidationStatus = Literal["valid", "invalid", "not_run"]
ValidationMode = Literal["static", "unit", "edge", "pipeline_sample"]
PipelineGenerationStrategy = Literal["pipeline_first", "node_first"]
GenerationJobStatus = Literal[
    "queued", "running", "valid", "invalid", "failed", "cancelled"
]
DeploymentValidationMode = Literal["fast", "validate", "repair", "validate-and-repair"]


class LLMConfig(BaseModel):
    """Per-request code-generation model configuration.

    The API key is injected from ``X-LLM-API-Key`` and excluded from every
    serialized request/job snapshot.
    """

    provider: str
    model: str
    base_url: str
    api_key: str = Field(default="", exclude=True, repr=False)
    timeout_seconds: int = 180
    model_family: str = "code"
    supports_function_calling: bool = True
    supports_json_output: bool = True
    supports_structured_output: bool = True
    supports_vision: bool = False


def is_input_node_kind(value: str) -> bool:
    """Return whether a legacy or canonical inLUMEN kind represents a source."""
    return value in {"input", "source"}


def is_output_node_kind(value: str) -> bool:
    """Return whether a legacy or canonical inLUMEN kind represents a destination."""
    return value in {"output", "destination"}


class FileSample(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    text: str | None = None
    omitted_bytes: int = 0
    data_uri: str | None = None
    preview_data_uris: list[str] = Field(default_factory=list)
    media_url: str | None = None
    mime_type: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FileDescriptor(BaseModel):
    filename: str
    bucket: str | None = None
    content_type: str | None = None
    kind: ArtifactKind | None = None
    format: str | None = None
    columns: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    schema: dict[str, Any] = Field(default_factory=dict)
    semantic_role: str = ""
    size_bytes: int | None = None
    sample: FileSample | None = None


class NodeDescriptor(BaseModel):
    flow_id: str
    label: str = ""
    description: str = ""
    type: NodeKind = "custom"
    template: str = ""
    ports: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    secret_parameters: list[str] = Field(default_factory=list)
    implementation: dict[str, Any] = Field(default_factory=dict)
    subpipeline: dict[str, Any] = Field(default_factory=dict)
    files: list[FileDescriptor] = Field(default_factory=list)


class GraphEdge(BaseModel):
    source: str
    target: str
    source_port: str = ""
    target_port: str = ""


class GraphContext(BaseModel):
    nodes: list[NodeDescriptor] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    upstream_nodes: list[str] = Field(default_factory=list)
    downstream_nodes: list[str] = Field(default_factory=list)


class RuntimeConstraints(BaseModel):
    language: Literal["python"] = "python"
    python_version: str = "3.11"
    base_image: str = "python:3.11-slim"
    allowed_packages: list[str] = Field(
        default_factory=lambda: [
            "pandas",
            "numpy",
            "pillow",
            "scikit-learn",
            "requests",
        ]
    )
    allow_unlisted_model_packages: bool = False
    network_allowed: bool = False
    max_runtime_seconds: int = 60


class ExpectedArtifact(BaseModel):
    name: str
    kind: ArtifactKind
    format: str | None = None
    description: str = ""
    filename: str | None = None
    columns: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    schema: dict[str, Any] = Field(default_factory=dict)
    semantic_role: str = ""


class GenerationContext(BaseModel):
    schema_version: str = "inlumen.script-generation-context@1"
    target_node: NodeDescriptor
    pipeline: dict[str, Any] = Field(default_factory=dict)
    graph: GraphContext = Field(default_factory=GraphContext)
    available_inputs: list[FileDescriptor] = Field(default_factory=list)
    expected_outputs: list[ExpectedArtifact] = Field(default_factory=list)
    runtime_constraints: RuntimeConstraints = Field(default_factory=RuntimeConstraints)


class GenerationOptions(BaseModel):
    persist: bool = True
    repair_attempts: int = 2
    include_sample_data: bool = False
    validation_mode: ValidationMode = "static"
    user_instruction: str = ""
    allow_deterministic_fallback: bool = Field(
        default_factory=lambda: (
            os.getenv(
                "CODEGEN_ALLOW_DETERMINISTIC_FALLBACK",
                "false",
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
    )
    generation_strategy: PipelineGenerationStrategy = "pipeline_first"
    # When populated, the pipeline service regenerates only these nodes and
    # replays validated bundles for the rest of the graph.  Keeping this on the
    # generation contract (rather than only filtering persistence) preserves
    # end-to-end contract and sample execution validation.
    target_flow_ids: list[str] = Field(default_factory=list)


class GenerateNodeScriptRequest(BaseModel):
    context: GenerationContext
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    llm_config: LLMConfig | None = None


class GeneratedFile(BaseModel):
    filename: str
    content: str
    content_type: str = "text/plain"


class DataContract(BaseModel):
    contract_id: str = "inlumen.generic-node@1"
    input_manifest_env: str = "PIPELINE_INPUT_DIR"
    output_dir_env: str = "PIPELINE_OUTPUT_DIR"
    output_manifest_env: str = ""
    context_path_env: str = ""
    inputs: list[ExpectedArtifact] = Field(default_factory=list)
    outputs: list[ExpectedArtifact] = Field(default_factory=list)


class ValidationReport(BaseModel):
    status: ValidationStatus
    checks: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GenerationUsage(BaseModel):
    """Provider-reported usage accumulated across one code-generation run."""

    request_count: int = 0
    usage_reported_count: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None

    def add(self, usage: "GenerationUsage") -> None:
        self.request_count += usage.request_count
        self.usage_reported_count += usage.usage_reported_count
        for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, field_name)
            if value is not None:
                current = getattr(self, field_name)
                setattr(self, field_name, (current or 0) + value)
        if usage.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + usage.cost_usd


class GeneratedArtifact(BaseModel):
    status: Literal["current", "stale"] = "current"
    generator: str = "inlumen-codegen-service"
    generator_version: str = "0.1.0"
    entrypoint: list[str] = Field(default_factory=lambda: ["python", "/app/main.py"])
    data_contract: DataContract
    files: list[GeneratedFile]
    validation_report: ValidationReport
    generation_usage: GenerationUsage | None = None


class GenerateNodeScriptResponse(BaseModel):
    flow_id: str
    generated_artifact: GeneratedArtifact


class ValidateNodeScriptRequest(BaseModel):
    context: GenerationContext
    files: list[GeneratedFile]


class PipelineGenerationContext(BaseModel):
    schema_version: str = "inlumen.pipeline-script-generation-context@1"
    pipeline: dict[str, Any] = Field(default_factory=dict)
    graph: GraphContext
    design: dict[str, Any] = Field(default_factory=dict)
    runtime_constraints: RuntimeConstraints = Field(default_factory=RuntimeConstraints)


class GeneratePipelineScriptsRequest(BaseModel):
    context: PipelineGenerationContext
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    llm_config: LLMConfig | None = None
    # Stored artifacts are supplied as JSON because PipelineGeneratedNode is
    # declared below this request type.  The generator validates every entry
    # before it is eligible for reuse.
    reusable_nodes: list[dict[str, Any]] = Field(default_factory=list)


class ResumePipelineGenerationRunRequest(BaseModel):
    flow_id: str | None = None
    repair_attempts: int | None = None
    user_instruction: str = ""
    llm_config: LLMConfig | None = None


class EdgeDataContract(BaseModel):
    source: str
    target: str
    outputs: list[ExpectedArtifact] = Field(default_factory=list)


class PipelineGeneratedNode(BaseModel):
    flow_id: str
    generated_artifact: GeneratedArtifact


class PipelineGenerationRunStep(BaseModel):
    flow_id: str
    status: Literal["pending", "running", "valid", "invalid", "failed", "skipped"] = (
        "pending"
    )
    stage: str = "pending"
    attempts: int = 1
    inputs: list[FileDescriptor] = Field(default_factory=list)
    outputs: list[FileDescriptor] = Field(default_factory=list)
    validation_report: ValidationReport | None = None


class PipelineGenerationRun(BaseModel):
    run_id: str
    status: Literal["running", "valid", "invalid", "failed", "cancelled"] = "running"
    mode: str = "sequential_node_handoff"
    steps: list[PipelineGenerationRunStep] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stage_timings_ms: dict[str, int] = Field(default_factory=dict)
    generation_usage: GenerationUsage | None = None
    current_stage: str = "pending"
    stage_started_at: str | None = None
    progress_updated_at: str | None = None
    progress_revision: int = 0


class GeneratePipelineScriptsResponse(BaseModel):
    nodes: list[PipelineGeneratedNode]
    edges: list[EdgeDataContract] = Field(default_factory=list)
    integration_validation: ValidationReport
    generation_run: PipelineGenerationRun | None = None


class PipelineGenerationJobResponse(BaseModel):
    run_id: str
    status: GenerationJobStatus
    resumed_from_run_id: str | None = None
    resume_from_flow_id: str | None = None
    generation_run: PipelineGenerationRun | None = None
    result: GeneratePipelineScriptsResponse | None = None
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DeploymentBundleValidationRequest(BaseModel):
    execution_id: str | None = Field(default=None, min_length=1, max_length=160)
    files: list[dict[str, Any]]
    targets: dict[str, bool] = Field(default_factory=dict)
    mode: DeploymentValidationMode = "validate"
    validate_argo: bool | None = None
    validate_dagster: bool | None = None
    materialize: bool = True
    reinstall: bool = False
    skip_install: bool = False
    argo_lint: bool = False
    argo_dry_run: bool = False
    timeout_seconds: int = Field(default=900, ge=30, le=3600)
    runtime_secrets: dict[str, str] = Field(default_factory=dict, repr=False)
