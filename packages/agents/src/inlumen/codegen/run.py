"""Generate Task packages for a pipeline: the entry point for other programs.

    from inlumen.codegen.run import generate, http_model
    from inlumen.codegen.sandbox import DockerSandbox

    result = await generate(
        design_result.graph,                      # a canvas graph
        model=http_model("openai/gpt-5.2-codex", base_url="https://openrouter.ai/api/v1",
                         api_key=key, provider="openrouter"),
        sandbox=DockerSandbox(),                  # default NoSandbox(): static checks only
        files="data/",                            # attached files, for samples and validation
    )
    result.accepted          # the integration validation passed
    result.packages          # {flow_id: {filename: content}}: one Task package per step
    result.requests          # one record per model request (tokens, cost, latency)

It runs the same planning, generation, repair and validation as the inLUMEN
codegen service. The service adds jobs, storage and authentication around it.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inlumen.contracts import package_version

from .context import Sampler, directory_sampler, pipeline_context
from .generator import generate_pipeline_script_bundles
from .llm import HttpModelClient
from .model import EventSink, ModelClient, use_events, use_model
from .sandbox import DockerSandbox, NoSandbox, Sandbox, use_sandbox
from .schemas import (
    GeneratePipelineScriptsRequest,
    GeneratePipelineScriptsResponse,
    GenerationOptions,
    LLMConfig,
    PipelineGenerationRun,
)

__all__ = [
    "CodegenResult",
    "DockerSandbox",
    "ModelClient",
    "NoSandbox",
    "generate",
    "http_model",
]


# What the inLUMEN app asks for (backend `_build_pipeline_codegen_payload`): run the
# whole pipeline on sample inputs when a sandbox is available. The deterministic
# fallback stays off, so a result measures the model.
APP_DEFAULTS: dict[str, Any] = {
    "allow_deterministic_fallback": False,
    "validation_mode": "pipeline_sample",
    "include_sample_data": True,
}


def http_model(
    model: str,
    *,
    base_url: str,
    api_key: str,
    provider: str = "openai",
    **config: Any,
) -> HttpModelClient:
    """A model behind an OpenAI-compatible chat-completions endpoint.

    ``provider="openrouter"`` adds OpenRouter's headers and records its
    reported cost. Other ``LLMConfig`` fields (``timeout_seconds``,
    ``max_output_tokens``, ``openrouter_provider_only``, …) pass through.
    """
    return HttpModelClient(LLMConfig(
        provider=provider, model=model, base_url=base_url, api_key=api_key, **config,
    ))


@dataclass
class CodegenResult:
    """The outcome of one generation run."""

    response: GeneratePipelineScriptsResponse  # everything the service returns
    requests: list[dict[str, Any]] = field(default_factory=list)
    package_version: str = ""

    @property
    def accepted(self) -> bool:
        """The generated pipeline passed integration validation."""
        return self.response.integration_validation.status == "valid"

    @property
    def validation(self) -> dict[str, Any]:
        return self.response.integration_validation.model_dump(mode="json")

    @property
    def packages(self) -> dict[str, dict[str, str]]:
        """One Task package per step: ``{flow_id: {filename: content}}``."""
        return {
            node.flow_id: {item.filename: item.content for item in node.generated_artifact.files}
            for node in self.response.nodes
        }

    @property
    def usage(self) -> dict[str, Any]:
        return {
            "requests": len(self.requests),
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in self.requests),
            "completion_tokens": sum(r.get("completion_tokens") or 0 for r in self.requests),
            "cost_usd": sum(r.get("cost_usd") or 0 for r in self.requests),
        }

    def write_packages(self, directory: str | Path) -> list[Path]:
        """Write each step's package to ``directory/<flow_id>/``; returns the files written."""
        root = Path(directory)
        written = []
        for flow_id, files in self.packages.items():
            for filename, content in files.items():
                path = (root / flow_id / filename).resolve()
                if root.resolve() not in path.parents:
                    raise ValueError(f"Generated filename escapes the package folder: {filename}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                written.append(path)
        return written


async def generate(
    graph: Mapping[str, Any],
    *,
    model: ModelClient | LLMConfig,
    sandbox: Sandbox | None = None,
    files: str | Path | Sampler | None = None,
    instruction: str = "",
    runtime: Mapping[str, Any] | None = None,
    options: GenerationOptions | Mapping[str, Any] | None = None,
    events: EventSink | None = None,
    progress: Callable[[PipelineGenerationRun], Awaitable[None] | None] | None = None,
) -> CodegenResult:
    """Generate and validate Task packages for a canvas ``graph``.

    ``model`` is a :class:`ModelClient` (see :func:`http_model`) or an
    ``LLMConfig``. ``sandbox`` defaults to :class:`NoSandbox`; pass
    :class:`DockerSandbox` to execute the pipeline on sample inputs.
    ``files`` is a folder holding attached files (``<folder>/<flow_id>/<name>``
    or ``<folder>/<name>``) or a sampler. ``runtime`` overrides the runtime
    constraints, e.g. ``{"allowed_packages": [...], "network_allowed": False}``
    for a fixed target environment.

    ``options`` default to what the inLUMEN app sends (:data:`APP_DEFAULTS`:
    sample-run validation, no deterministic fallback, so results measure the
    model); fields set in ``options`` take precedence.
    """
    sampler = directory_sampler(files) if isinstance(files, (str, Path)) else files
    context = pipeline_context(dict(graph), sample=sampler)
    context["runtime_constraints"].update(runtime or {})
    # Fields a caller set explicitly win over the app's defaults, whether
    # `options` is a dict or a GenerationOptions.
    explicit = (options.model_dump(exclude_unset=True) if isinstance(options, GenerationOptions)
                else dict(options or {}))
    resolved_options = GenerationOptions(**{**APP_DEFAULTS, **explicit})
    if instruction:
        resolved_options.user_instruction = instruction
    resolved_options.persist = False

    if isinstance(model, LLMConfig):
        client: ModelClient = HttpModelClient(model)
        llm_config = model
    else:
        client = model
        llm_config = model.config if isinstance(model, HttpModelClient) else LLMConfig(
            provider="custom", model=str(getattr(model, "name", "") or "custom"), base_url="",
        )

    request = GeneratePipelineScriptsRequest(
        context=context, options=resolved_options, llm_config=llm_config,
    )
    requests: list[dict[str, Any]] = []

    def record(event: dict[str, Any]) -> None:
        requests.append(event)
        if events is not None:
            events(event)

    with use_model(client), use_events(record), use_sandbox(sandbox or NoSandbox()):
        response = await generate_pipeline_script_bundles(request, progress_callback=progress)
    return CodegenResult(response=response, requests=requests, package_version=package_version())
