from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .llm import (
    LLMGenerationError,
    generate_node_payload,
    generate_pipeline_payload,
    repair_node_payload,
    repair_pipeline_payload,
)
from .pipeline_compiler import (
    PipelineCompilerError,
    compile_pipeline_nodes,
    compose_pipeline_program,
    deterministic_pipeline_source,
    function_name_for_flow_id,
    isolate_pipeline_nodes,
    validate_compiled_equivalence,
    validate_pipeline_source,
)
from .sandbox import (
    execute_node_with_docker_handoff,
    validate_node_with_docker,
    validate_pipeline_dependencies_with_docker,
    validate_pipeline_program_with_docker,
    validate_pipeline_with_docker,
    validation_workspace,
)
from .schemas import (
    DataContract,
    EdgeDataContract,
    ExpectedArtifact,
    FileDescriptor,
    GeneratedArtifact,
    GeneratedFile,
    GenerateNodeScriptRequest,
    GenerateNodeScriptResponse,
    GeneratePipelineScriptsRequest,
    GeneratePipelineScriptsResponse,
    GenerationContext,
    GenerationUsage,
    GraphContext,
    PipelineGeneratedNode,
    PipelineGenerationRun,
    PipelineGenerationRunStep,
    ValidationReport,
    is_input_node_kind,
    is_output_node_kind,
)
from .task_profiles import (
    classify_node_task,
    implementation_plan_for_node,
    required_packages_for_node,
    source_semantic_errors,
    task_profile_payload,
)
from .trusted_adapters import apply_trusted_adapters
from .validation import (
    imported_roots_from_python,
    package_name,
    requirement_name_for_import,
    validate_generated_files,
)

GENERATOR_VERSION = "0.1.0"

PipelineProgressCallback = Callable[[PipelineGenerationRun], Awaitable[None] | None]
GenerationUsageCallback = Callable[[GenerationUsage], Awaitable[None] | None]


async def emit_pipeline_progress(
    callback: PipelineProgressCallback | None,
    run: PipelineGenerationRun,
) -> None:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    running_step = next(
        (step for step in reversed(run.steps) if step.status == "running"),
        None,
    )
    if running_step is not None and run.current_stage != running_step.stage:
        run.current_stage = running_step.stage
        run.stage_started_at = now
    if run.stage_started_at is None:
        run.stage_started_at = now
    run.progress_updated_at = now
    run.progress_revision += 1
    if callback is None:
        return
    result = callback(run)
    if inspect.isawaitable(result):
        await result


async def set_running_pipeline_stage(
    run: PipelineGenerationRun,
    stage: str,
    callback: PipelineProgressCallback | None,
) -> None:
    if run.current_stage != stage:
        run.current_stage = stage
        run.stage_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for step in run.steps:
        if step.status == "running":
            step.stage = stage
    await emit_pipeline_progress(callback, run)


def threadsafe_stage_callback(
    loop: asyncio.AbstractEventLoop,
    update_stage: Callable[[str], Awaitable[None]],
) -> Callable[[str], None]:
    def callback(stage: str) -> None:
        future = asyncio.run_coroutine_threadsafe(update_stage(stage), loop)
        future.result(timeout=10)

    return callback


async def generate_node_script_bundle(
    request: GenerateNodeScriptRequest,
    *,
    usage_callback: GenerationUsageCallback | None = None,
) -> GenerateNodeScriptResponse:
    used_fallback = False
    generation_usage = GenerationUsage()

    async def record_usage(usage: GenerationUsage) -> None:
        generation_usage.add(usage)
        if usage_callback is not None:
            callback_result = usage_callback(usage)
            if inspect.isawaitable(callback_result):
                await callback_result

    if request.llm_config is None:
        if not request.options.allow_deterministic_fallback:
            raise LLMGenerationError("Code-generation model configuration is required.")
        payload = fallback_script_payload(request)
        used_fallback = True
    else:
        try:
            payload = await generate_node_payload(
                request.llm_config,
                request,
                record_usage,
            )
        except LLMGenerationError:
            if not request.options.allow_deterministic_fallback:
                raise
            payload = fallback_script_payload(request)
            used_fallback = True

    files, data_contract, validation = await build_and_validate_node(request, payload)
    attempts = 1
    while (
        validation.status == "invalid"
        and request.llm_config is not None
        and attempts <= request.options.repair_attempts
    ):
        try:
            payload = await repair_node_payload(
                request.llm_config,
                request,
                payload,
                validation,
                record_usage,
            )
        except LLMGenerationError:
            if not request.options.allow_deterministic_fallback:
                raise
            payload = fallback_script_payload(request)
            used_fallback = True
        attempts += 1
        files, data_contract, validation = await build_and_validate_node(
            request,
            payload,
        )

    if validation.status == "invalid" and request.options.allow_deterministic_fallback:
        payload = fallback_script_payload(request)
        used_fallback = True
        files, data_contract, validation = await build_and_validate_node(request, payload)

    if used_fallback:
        validation.warnings.append(
            "The explicitly enabled deterministic fallback was used after AI generation was unavailable or invalid."
        )
    else:
        validation.warnings.append(
            f"Generated by the configured coding model {request.llm_config.model} in {attempts} attempt(s)."
        )
    files.append(
        GeneratedFile(
            filename="validation-report.json",
            content=validation.model_dump_json(indent=2) + "\n",
            content_type="application/json",
        )
    )

    artifact = GeneratedArtifact(
        generator="inlumen-codegen-service",
        generator_version=GENERATOR_VERSION,
        data_contract=data_contract,
        files=files,
        validation_report=validation,
        generation_usage=(
            generation_usage if generation_usage.request_count > 0 else None
        ),
    )
    return GenerateNodeScriptResponse(
        flow_id=request.context.target_node.flow_id,
        generated_artifact=artifact,
    )


async def build_and_validate_node(
    request: GenerateNodeScriptRequest,
    payload: dict[str, Any],
) -> tuple[list[GeneratedFile], DataContract, ValidationReport]:
    files = files_from_payload(request, payload)
    data_contract = data_contract_from_payload(request, payload)
    validation = validate_generated_files(
        files=files,
        runtime_constraints=request.context.runtime_constraints,
    )
    merge_validation_report(
        validation,
        validate_contract_alignment(
            expected_outputs=request.context.expected_outputs,
            actual=data_contract,
        ),
    )
    main_source = next(
        (item.content for item in files if item.filename == "main.py"),
        "",
    )
    semantic_errors = source_semantic_errors(
        request.context.target_node,
        request.context.available_inputs,
        main_source,
    )
    merge_validation_report(
        validation,
        ValidationReport(
            status="invalid" if semantic_errors else "valid",
            checks=["reviewed_implementation_plan_alignment"],
            errors=semantic_errors,
        ),
    )
    if request.options.validation_mode in {"unit", "edge", "pipeline_sample"}:
        execution_report = await asyncio.to_thread(
            validate_node_with_docker,
            flow_id=request.context.target_node.flow_id,
            artifact=GeneratedArtifact(
                generator="inlumen-codegen-service",
                generator_version=GENERATOR_VERSION,
                data_contract=data_contract,
                files=files,
                validation_report=validation,
            ),
            input_files=request.context.available_inputs,
            parameters=request.context.target_node.parameters,
            timeout_seconds=request.context.runtime_constraints.max_runtime_seconds,
        )
        merge_validation_report(validation, execution_report)
    return files, data_contract, validation


async def generate_pipeline_script_bundles(
    request: GeneratePipelineScriptsRequest,
    *,
    run_id: str | None = None,
    progress_callback: PipelineProgressCallback | None = None,
    start_from_flow_id: str | None = None,
    seed_nodes: list[PipelineGeneratedNode] | None = None,
) -> GeneratePipelineScriptsResponse:
    """Generate one canonical pipeline and compile it into node bundles."""
    target_flow_ids = {
        str(flow_id).strip()
        for flow_id in request.options.target_flow_ids
        if str(flow_id).strip()
    }
    request_reusable_nodes = [
        PipelineGeneratedNode.model_validate(item)
        for item in request.reusable_nodes
        if isinstance(item, dict)
    ]
    reusable_by_flow_id = {
        item.flow_id: item for item in request_reusable_nodes
    }
    reusable_by_flow_id.update({
        item.flow_id: item for item in seed_nodes or []
    })
    if request.options.generation_strategy == "node_first" or target_flow_ids:
        return await generate_pipeline_script_bundles_node_first(
            request,
            run_id=run_id,
            progress_callback=progress_callback,
            start_from_flow_id=start_from_flow_id,
            seed_nodes=list(reusable_by_flow_id.values()),
            target_flow_ids=target_flow_ids or None,
        )
    return await generate_pipeline_script_bundles_pipeline_first(
        request,
        run_id=run_id,
        progress_callback=progress_callback,
    )


async def generate_pipeline_script_bundles_pipeline_first(
    request: GeneratePipelineScriptsRequest,
    *,
    run_id: str | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> GeneratePipelineScriptsResponse:
    plan, contexts_by_node = build_pipeline_generation_plan(request)
    run = PipelineGenerationRun(
        run_id=run_id or uuid.uuid4().hex,
        mode="pipeline_first_single_script",
        steps=[
            PipelineGenerationRunStep(
                flow_id=node["flow_id"],
                status="running",
                stage="pipeline_planning",
                inputs=contexts_by_node[node["flow_id"]].available_inputs,
            )
            for node in plan["nodes"]
        ],
        generation_usage=GenerationUsage(),
    )

    async def record_usage(usage: GenerationUsage) -> None:
        if run.generation_usage is None:
            run.generation_usage = GenerationUsage()
        run.generation_usage.add(usage)
        await emit_pipeline_progress(progress_callback, run)

    await emit_pipeline_progress(progress_callback, run)

    await set_running_pipeline_stage(run, "pipeline_generation", progress_callback)
    generation_started = time.perf_counter()
    used_fallback = False
    if request.llm_config is None:
        if not request.options.allow_deterministic_fallback:
            raise LLMGenerationError("Code-generation model configuration is required.")
        payload = deterministic_pipeline_payload(plan)
        used_fallback = True
    else:
        try:
            payload = await generate_pipeline_payload(
                request.llm_config,
                plan,
                request.options.user_instruction,
                record_usage,
            )
        except LLMGenerationError:
            if not request.options.allow_deterministic_fallback:
                raise
            payload = deterministic_pipeline_payload(plan)
            used_fallback = True
    run.stage_timings_ms["pipeline_generation"] = round(
        (time.perf_counter() - generation_started) * 1000
    )
    payload = normalize_pipeline_payload(payload, plan)

    await set_running_pipeline_stage(run, "pipeline_validation", progress_callback)
    validation_started = time.perf_counter()
    attempt = 1
    while True:
        for step in run.steps:
            step.attempts = attempt
        (
            _requirements,
            compiled_by_node,
            integration_validation,
        ) = await evaluate_pipeline_draft(
            request,
            plan,
            payload,
            run=run,
            progress_callback=progress_callback,
        )
        if integration_validation.status == "valid":
            break
        if request.llm_config is None or attempt > request.options.repair_attempts:
            break
        await set_running_pipeline_stage(run, "pipeline_repair", progress_callback)
        try:
            payload = await repair_pipeline_payload(
                request.llm_config,
                plan,
                payload,
                integration_validation,
                request.options.user_instruction,
                record_usage,
            )
        except LLMGenerationError:
            if not request.options.allow_deterministic_fallback:
                raise
            payload = deterministic_pipeline_payload(plan)
            used_fallback = True
        payload = normalize_pipeline_payload(payload, plan)
        attempt += 1
        await set_running_pipeline_stage(run, "pipeline_validation", progress_callback)

    if (
        integration_validation.status == "invalid"
        and request.options.allow_deterministic_fallback
        and not used_fallback
    ):
        payload = normalize_pipeline_payload(deterministic_pipeline_payload(plan), plan)
        used_fallback = True
        attempt += 1
        for step in run.steps:
            step.attempts = attempt
        (
            _requirements,
            compiled_by_node,
            integration_validation,
        ) = await evaluate_pipeline_draft(
            request,
            plan,
            payload,
            run=run,
            progress_callback=progress_callback,
        )
    run.stage_timings_ms["pipeline_validation"] = round(
        (time.perf_counter() - validation_started) * 1000
    )
    if used_fallback:
        integration_validation.warnings.append(
            "The explicitly enabled deterministic fallback was used after AI generation was unavailable or invalid."
        )
    else:
        integration_validation.warnings.append(
            f"Generated by the configured coding model {request.llm_config.model} in {attempt} attempt(s)."
        )

    generated_nodes: list[PipelineGeneratedNode] = []
    notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []
    await set_running_pipeline_stage(run, "node_compilation", progress_callback)
    compilation_started = time.perf_counter()
    for step, node_plan in zip(run.steps, plan["nodes"]):
        flow_id = node_plan["flow_id"]
        context = contexts_by_node[flow_id]
        source = compiled_by_node.get(flow_id)
        if not source:
            source = compile_failure_node_source(flow_id)
        node_requirements = requirements_for_compiled_source(
            payload.get("requirements"),
            context.runtime_constraints.allowed_packages,
            source=source,
            required_packages=required_packages_for_node(
                context.target_node,
                context.available_inputs,
            ),
        )
        node_payload = {
            "main_py": source,
            "requirements": node_requirements,
            "implementation_plan": node_plan.get("implementation_plan") or {},
            "outputs": node_plan["outputs"],
            "notes": [
                *notes,
                "Compiled from one validated canonical pipeline program.",
            ],
        }
        files = files_from_payload(
            GenerateNodeScriptRequest(
                context=context,
                options=request.options.model_copy(
                    update={"validation_mode": "static"}
                ),
            ),
            node_payload,
        )
        contract = data_contract_from_payload(
            GenerateNodeScriptRequest(context=context),
            node_payload,
        )
        validation = validate_generated_files(
            files=files,
            runtime_constraints=context.runtime_constraints,
        )
        merge_validation_report(
            validation,
            validate_contract_alignment(
                expected_outputs=context.expected_outputs,
                actual=contract,
            ),
        )
        if integration_validation.status == "invalid":
            merge_validation_report(
                validation,
                ValidationReport(
                    status="invalid",
                    checks=["canonical_pipeline_validation"],
                    errors=list(integration_validation.errors),
                    warnings=list(integration_validation.warnings),
                ),
            )
        else:
            validation.checks.extend(
                check
                for check in (
                    "canonical_pipeline_validation",
                    "compiled_node_independence",
                )
                if check not in validation.checks
            )
        files.append(
            GeneratedFile(
                filename="validation-report.json",
                content=validation.model_dump_json(indent=2) + "\n",
                content_type="application/json",
            )
        )
        artifact = GeneratedArtifact(
            generator="inlumen-codegen-service",
            generator_version=GENERATOR_VERSION,
            data_contract=contract,
            files=files,
            validation_report=validation,
        )
        generated_nodes.append(
            PipelineGeneratedNode(
                flow_id=flow_id,
                generated_artifact=artifact,
            )
        )
        step.validation_report = validation
        step.status = "valid" if validation.status == "valid" else "invalid"
        step.stage = (
            "compiled_independent_bundle"
            if step.status == "valid"
            else "pipeline_validation_failed"
        )
        await emit_pipeline_progress(progress_callback, run)
    run.stage_timings_ms["node_compilation"] = round(
        (time.perf_counter() - compilation_started) * 1000
    )

    invalid_nodes = [
        item.flow_id
        for item in generated_nodes
        if item.generated_artifact.validation_report.status != "valid"
    ]
    if invalid_nodes and integration_validation.status != "invalid":
        integration_validation.status = "invalid"
        integration_validation.errors.extend(
            f"Compiled node {flow_id} is invalid." for flow_id in invalid_nodes
        )
    run.status = "invalid" if integration_validation.status == "invalid" else "valid"
    run.current_stage = "complete"
    run.stage_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    run.errors = list(integration_validation.errors)
    run.warnings = list(integration_validation.warnings)
    await emit_pipeline_progress(progress_callback, run)
    return GeneratePipelineScriptsResponse(
        nodes=generated_nodes,
        edges=build_edge_contracts(request.context.graph, generated_nodes),
        integration_validation=integration_validation,
        generation_run=run,
    )


def build_pipeline_generation_plan(
    request: GeneratePipelineScriptsRequest,
) -> tuple[dict[str, Any], dict[str, GenerationContext]]:
    graph = request.context.graph
    ordered_ids = topological_order(graph)
    nodes_by_id = {node.flow_id: node for node in graph.nodes}
    outputs_by_node: dict[str, list[ExpectedArtifact]] = {}
    contexts: dict[str, GenerationContext] = {}
    plan_nodes: list[dict[str, Any]] = []
    for flow_id in ordered_ids:
        node = nodes_by_id[flow_id]
        if node.type == "config":
            continue
        parent_ids = [
            edge.source
            for edge in graph.edges
            if edge.target == flow_id and edge.source in outputs_by_node
        ]
        child_ids = [edge.target for edge in graph.edges if edge.source == flow_id]
        inherited_inputs = [
            FileDescriptor(
                filename=artifact_filename(parent_id, output),
                kind=output.kind,
                format=output.format,
                columns=output.columns,
                required_columns=output.required_columns,
                schema=output.schema,
                semantic_role=output.semantic_role,
            )
            for parent_id in parent_ids
            for output in outputs_by_node[parent_id]
        ]
        available_inputs = [*node.files, *inherited_inputs]
        expected_outputs = expected_outputs_for_node(
            node,
            child_ids,
            available_inputs,
        )
        outputs_by_node[flow_id] = expected_outputs
        context = GenerationContext(
            target_node=node,
            pipeline={
                **request.context.pipeline,
                "generation_mode": "pipeline_first",
                "upstream_contracts": [
                    {
                        "source_node_id": parent_id,
                        "outputs": [
                            output.model_dump(mode="json")
                            for output in outputs_by_node[parent_id]
                        ],
                    }
                    for parent_id in parent_ids
                ],
            },
            graph=GraphContext(
                nodes=graph.nodes,
                edges=graph.edges,
                upstream_nodes=parent_ids,
                downstream_nodes=child_ids,
            ),
            available_inputs=available_inputs,
            expected_outputs=expected_outputs,
            runtime_constraints=request.context.runtime_constraints,
        )
        contexts[flow_id] = context
        plan_nodes.append(
            {
                "flow_id": flow_id,
                "function_name": function_name_for_flow_id(flow_id),
                "parents": parent_ids,
                "children": child_ids,
                "input_filenames": [descriptor.filename for descriptor in node.files],
                "inputs": [
                    descriptor.model_dump(mode="json")
                    for descriptor in available_inputs
                ],
                "outputs": [
                    output.model_dump(mode="json") for output in expected_outputs
                ],
                "descriptor": node.model_dump(mode="json", exclude={"files"}),
                "task_profile": task_profile_payload(node, available_inputs),
                "implementation_plan": implementation_plan_for_node(
                    node, available_inputs
                ),
            }
        )
    required_packages = merge_requirements(
        *[
            required_packages_for_node(
                context.target_node,
                context.available_inputs,
            )
            for context in contexts.values()
        ]
    )
    allowed_packages = merge_requirements(
        request.context.runtime_constraints.allowed_packages,
        required_packages,
    )
    for context in contexts.values():
        context.runtime_constraints = context.runtime_constraints.model_copy(
            update={"allowed_packages": allowed_packages}
        )
    return (
        {
            "schema_version": "inlumen.pipeline-plan@1",
            "pipeline": request.context.pipeline,
            "design": request.context.design,
            "edges": [edge.model_dump(mode="json") for edge in graph.edges],
            "nodes": plan_nodes,
            "required_packages": required_packages,
        },
        contexts,
    )


async def evaluate_pipeline_draft(
    request: GeneratePipelineScriptsRequest,
    plan: dict[str, Any],
    payload: dict[str, Any],
    *,
    run: PipelineGenerationRun | None = None,
    progress_callback: PipelineProgressCallback | None = None,
) -> tuple[list[str], dict[str, str], ValidationReport]:
    checks = [
        "single_pipeline_generation",
        "pipeline_payload_shape",
        "canonical_pipeline_static_validation",
        "canonical_pipeline_compilation",
    ]
    validation = ValidationReport(status="valid", checks=checks)
    payload_error = pipeline_payload_shape_error(payload, plan)
    if payload_error:
        validation.errors.append(payload_error)

    source = str(payload.get("pipeline_py") or "").strip()
    required_packages = [
        str(item) for item in plan.get("required_packages") or [] if str(item).strip()
    ]
    allowed_packages = merge_requirements(
        request.context.runtime_constraints.allowed_packages,
        required_packages,
    )
    requirements = normalize_requirements(
        payload.get("requirements"),
        allowed_packages,
        main_py=source,
        required_packages=required_packages,
    )
    allowed_requirements = {
        package_name(item) for item in allowed_packages if package_name(item)
    }
    declared_requirements = {
        package_name(item) for item in requirements if package_name(item)
    }
    validation.checks.append("pipeline_dependency_allowlist")
    for requirement in sorted(declared_requirements):
        if allowed_requirements and requirement not in allowed_requirements:
            validation.errors.append(
                "Pipeline requirement is not in the allowed package list: "
                f"{requirement}"
            )
    for import_root in sorted(imported_roots_from_python(source)):
        requirement = requirement_name_for_import(import_root)
        if (
            requirement
            and allowed_requirements
            and requirement not in allowed_requirements
        ):
            validation.errors.append(
                "Pipeline imports a package that is not allowed: "
                f"{import_root} ({requirement})"
            )
        elif requirement and requirement not in declared_requirements:
            validation.errors.append(
                "Pipeline imports third-party package "
                f"{import_root} but requirements are missing {requirement}"
            )
    node_functions = {
        str(node["flow_id"]): str(node["function_name"]) for node in plan["nodes"]
    }
    source_validation = validate_pipeline_source(source, node_functions)
    merge_validation_report(validation, source_validation)
    compiled_by_node: dict[str, str] = {}
    pipeline_program = ""
    if source_validation.status == "valid":
        try:
            compiled_nodes = compile_pipeline_nodes(source, node_functions)
            compiled_by_node = {item.flow_id: item.source for item in compiled_nodes}
            merge_validation_report(
                validation,
                validate_compiled_equivalence(
                    source,
                    compiled_nodes,
                    node_functions,
                ),
            )
            validation.checks.append("reviewed_implementation_plan_alignment")
            nodes_by_id = {node.flow_id: node for node in request.context.graph.nodes}
            for node_plan in plan["nodes"]:
                flow_id = str(node_plan["flow_id"])
                node = nodes_by_id[flow_id]
                inputs = [
                    FileDescriptor.model_validate(item)
                    for item in node_plan.get("inputs") or []
                ]
                validation.errors.extend(
                    source_semantic_errors(
                        node,
                        inputs,
                        compiled_by_node[flow_id],
                        expected_outputs=[
                            ExpectedArtifact.model_validate(item)
                            for item in node_plan.get("outputs") or []
                        ],
                        function_name=str(node_plan["function_name"]),
                    )
                )
            pipeline_program = compose_pipeline_program(source, plan)
        except PipelineCompilerError as exc:
            validation.errors.append(f"Pipeline compilation failed: {exc}")

    if validation.errors:
        validation.status = "invalid"
    elif request.options.validation_mode in {
        "unit",
        "edge",
        "pipeline_sample",
    }:
        defer_external_models = any(
            str((node.get("implementation_plan") or {}).get("model_id") or "").strip()
            for node in plan["nodes"]
        )
        input_files = deduplicated_pipeline_inputs(request)
        stage_callback = None
        if run is not None:
            loop = asyncio.get_running_loop()

            async def update_stage(stage: str) -> None:
                await set_running_pipeline_stage(
                    run,
                    stage,
                    progress_callback,
                )

            stage_callback = threadsafe_stage_callback(loop, update_stage)

        if defer_external_models:
            if run is not None:
                await set_running_pipeline_stage(
                    run,
                    "dependency_validation",
                    progress_callback,
                )
            dependency_validation = await asyncio.to_thread(
                validate_pipeline_dependencies_with_docker,
                requirements=requirements,
                base_image=request.context.runtime_constraints.base_image,
            )
            merge_validation_report(validation, dependency_validation)

            executable_plan = model_free_executable_subplan(plan)
            if executable_plan["nodes"]:
                selected_flow_ids = {
                    str(node["flow_id"]) for node in executable_plan["nodes"]
                }
                isolated_source = isolate_pipeline_nodes(
                    source,
                    node_functions,
                    selected_flow_ids,
                )
                executable_program = compose_pipeline_program(
                    isolated_source,
                    executable_plan,
                )
                executable_requirements = requirements_for_model_free_subplan(
                    request,
                    executable_plan,
                    requirements,
                    isolated_source,
                    allowed_packages,
                )
                execution = await asyncio.to_thread(
                    validate_pipeline_program_with_docker,
                    pipeline_source=executable_program,
                    plan=executable_plan,
                    requirements=executable_requirements,
                    input_files=input_files,
                    base_image=request.context.runtime_constraints.base_image,
                    timeout_seconds=(
                        request.context.runtime_constraints.max_runtime_seconds
                        * max(1, len(executable_plan["nodes"]))
                    ),
                    network_allowed=False,
                    run_id=run.run_id if run is not None else None,
                    stage_callback=stage_callback,
                )
                if "model_free_subpipeline_sample_run" not in execution.checks:
                    execution.checks.append("model_free_subpipeline_sample_run")
                execution.warnings.append(
                    "Executed the model-free portion of the pipeline against "
                    "attached Source input files; external model nodes remain deferred."
                )
                merge_validation_report(validation, execution)
        else:
            execution = await asyncio.to_thread(
                validate_pipeline_program_with_docker,
                pipeline_source=pipeline_program,
                plan=plan,
                requirements=requirements,
                input_files=input_files,
                base_image=request.context.runtime_constraints.base_image,
                timeout_seconds=(
                    request.context.runtime_constraints.max_runtime_seconds
                    * max(1, len(plan["nodes"]))
                ),
                network_allowed=request.context.runtime_constraints.network_allowed,
                run_id=run.run_id if run is not None else None,
                stage_callback=stage_callback,
            )
            merge_validation_report(validation, execution)
    return requirements, compiled_by_node, validation


def model_free_executable_subplan(plan: dict[str, Any]) -> dict[str, Any]:
    """Select nodes whose complete dependency closure has no external model."""
    selected: set[str] = set()
    selected_nodes: list[dict[str, Any]] = []
    for node in plan.get("nodes") or []:
        flow_id = str(node.get("flow_id") or "")
        parents = {str(parent) for parent in node.get("parents") or []}
        model_id = str(
            (node.get("implementation_plan") or {}).get("model_id") or ""
        ).strip()
        if model_id or not parents.issubset(selected):
            continue
        selected.add(flow_id)
        selected_nodes.append(node)

    subset = dict(plan)
    subset["nodes"] = selected_nodes
    subset["edges"] = [
        edge
        for edge in plan.get("edges") or []
        if str(edge.get("source") or "") in selected
        and str(edge.get("target") or "") in selected
    ]
    return subset


def requirements_for_model_free_subplan(
    request: GeneratePipelineScriptsRequest,
    plan: dict[str, Any],
    requirements: list[str],
    source: str,
    allowed_packages: list[str],
) -> list[str]:
    nodes_by_id = {node.flow_id: node for node in request.context.graph.nodes}
    required: list[list[str]] = []
    for node_plan in plan.get("nodes") or []:
        flow_id = str(node_plan["flow_id"])
        inputs = [
            FileDescriptor.model_validate(item)
            for item in node_plan.get("inputs") or []
        ]
        required.append(
            required_packages_for_node(nodes_by_id[flow_id], inputs)
        )
    return requirements_for_compiled_source(
        requirements,
        allowed_packages,
        source=source,
        required_packages=merge_requirements(*required),
    )


def deterministic_pipeline_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "pipeline_py": deterministic_pipeline_source(plan),
        "requirements": [],
        "nodes": [
            {
                "flow_id": node["flow_id"],
                "function_name": node["function_name"],
            }
            for node in plan["nodes"]
        ],
        "notes": ["Deterministic pipeline-level fallback implementation."],
    }


def pipeline_payload_shape_error(
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> str | None:
    if not isinstance(payload, dict):
        return "Pipeline generation payload must be an object."
    if not str(payload.get("pipeline_py") or "").strip():
        return "Pipeline generation payload is missing non-empty pipeline_py."
    return None


def normalize_pipeline_payload(
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Keep compiler-owned metadata out of the model repair loop."""
    normalized = dict(payload) if isinstance(payload, dict) else {}
    if not isinstance(normalized.get("requirements"), list):
        normalized["requirements"] = []
    if not isinstance(normalized.get("notes"), list):
        normalized["notes"] = []
    source = str(normalized.get("pipeline_py") or "")
    if source.strip():
        normalized["pipeline_py"] = apply_trusted_adapters(source, plan)
    normalized["nodes"] = [
        {
            "flow_id": node["flow_id"],
            "function_name": node["function_name"],
        }
        for node in plan["nodes"]
    ]
    return normalized


def deduplicated_pipeline_inputs(
    request: GeneratePipelineScriptsRequest,
) -> list[FileDescriptor]:
    files: list[FileDescriptor] = []
    seen: set[tuple[str, str]] = set()
    for node in request.context.graph.nodes:
        for descriptor in node.files:
            key = (node.flow_id, descriptor.filename)
            if key not in seen:
                files.append(descriptor)
                seen.add(key)
    return files


def compile_failure_node_source(flow_id: str) -> str:
    return "\n".join(
        [
            "def main():",
            (
                "    raise RuntimeError("
                f"{f'Canonical pipeline compilation failed for {flow_id!r}'!r})"
            ),
            "",
            'if __name__ == "__main__":',
            "    main()",
            "",
        ]
    )


async def generate_pipeline_script_bundles_node_first(
    request: GeneratePipelineScriptsRequest,
    *,
    run_id: str | None = None,
    progress_callback: PipelineProgressCallback | None = None,
    start_from_flow_id: str | None = None,
    seed_nodes: list[PipelineGeneratedNode] | None = None,
    target_flow_ids: set[str] | None = None,
) -> GeneratePipelineScriptsResponse:
    ordered_ids = topological_order(request.context.graph)
    nodes_by_id = {node.flow_id: node for node in request.context.graph.nodes}
    if start_from_flow_id:
        if start_from_flow_id not in nodes_by_id:
            raise ValueError(
                f"Cannot resume pipeline generation: node {start_from_flow_id} not found"
            )
        if nodes_by_id[start_from_flow_id].type == "config":
            raise ValueError("Cannot resume pipeline generation from a config node")
    if target_flow_ids:
        unknown_targets = sorted(target_flow_ids.difference(nodes_by_id))
        if unknown_targets:
            raise ValueError(
                "Cannot target pipeline generation for unknown nodes: "
                + ", ".join(unknown_targets)
            )
        config_targets = sorted(
            flow_id
            for flow_id in target_flow_ids
            if nodes_by_id[flow_id].type == "config"
        )
        if config_targets:
            raise ValueError(
                "Config nodes cannot receive runtime bundles: "
                + ", ".join(config_targets)
            )
    run = PipelineGenerationRun(
        run_id=run_id or uuid.uuid4().hex,
        current_stage="preparing_nodes",
    )

    async def record_usage(usage: GenerationUsage) -> None:
        if run.generation_usage is None:
            run.generation_usage = GenerationUsage()
        run.generation_usage.add(usage)
        await emit_pipeline_progress(progress_callback, run)

    edge_contracts: list[EdgeDataContract] = []
    outputs_by_node: dict[str, list[ExpectedArtifact]] = {}
    handoff_outputs_by_node: dict[str, list[FileDescriptor]] = {}
    generated_nodes: list[PipelineGeneratedNode] = []
    reusable_artifacts = {
        item.flow_id: item.generated_artifact
        for item in seed_nodes or []
        if item.generated_artifact.validation_report.status == "valid"
    }
    resume_started = start_from_flow_id is None

    await emit_pipeline_progress(progress_callback, run)

    with validation_workspace(f"pipeline-run-{run.run_id}") as handoff_tmp:
        handoff_root = Path(handoff_tmp)
        for flow_id in ordered_ids:
            node = nodes_by_id[flow_id]
            if node.type == "config":
                continue
            is_before_resume_target = (
                not resume_started and flow_id != start_from_flow_id
            )
            is_outside_target_scope = bool(
                target_flow_ids and flow_id not in target_flow_ids
            )
            if flow_id == start_from_flow_id:
                resume_started = True

            parent_ids = [
                edge.source
                for edge in request.context.graph.edges
                if edge.target == flow_id and edge.source in outputs_by_node
            ]
            child_ids = [
                edge.target
                for edge in request.context.graph.edges
                if edge.source == flow_id
            ]
            upstream_contracts = [
                {
                    "source_node_id": parent_id,
                    "outputs": [
                        output.model_dump(mode="json")
                        for output in outputs_by_node.get(parent_id, [])
                    ],
                }
                for parent_id in parent_ids
            ]
            inherited_inputs: list[FileDescriptor] = []
            for parent_id in parent_ids:
                inherited_inputs.extend(handoff_outputs_by_node.get(parent_id, []))
                if parent_id not in handoff_outputs_by_node:
                    for output in outputs_by_node.get(parent_id, []):
                        inherited_inputs.append(
                            FileDescriptor(
                                filename=artifact_filename(parent_id, output),
                                kind=output.kind,
                                format=output.format,
                                columns=output.columns,
                                required_columns=output.required_columns,
                                schema=output.schema,
                                semantic_role=output.semantic_role,
                                sample=None,
                            )
                        )
            available_inputs = [*node.files, *inherited_inputs]
            node_required_packages = required_packages_for_node(
                node,
                available_inputs,
            )
            node_runtime_constraints = request.context.runtime_constraints.model_copy(
                update={
                    "allowed_packages": merge_requirements(
                        request.context.runtime_constraints.allowed_packages,
                        node_required_packages,
                    )
                }
            )
            context = GenerationContext(
                target_node=node,
                pipeline={
                    **request.context.pipeline,
                    "upstream_contracts": upstream_contracts,
                    "generation_mode": "pipeline",
                    "generation_run_id": run.run_id,
                },
                graph=GraphContext(
                    nodes=request.context.graph.nodes,
                    edges=request.context.graph.edges,
                    upstream_nodes=parent_ids,
                    downstream_nodes=child_ids,
                ),
                available_inputs=available_inputs,
                expected_outputs=expected_outputs_for_node(
                    node,
                    child_ids,
                    available_inputs,
                ),
                runtime_constraints=node_runtime_constraints,
            )

            if is_before_resume_target or is_outside_target_scope:
                artifact = reusable_artifacts.get(flow_id)
                if artifact is None:
                    raise ValueError(
                        "Cannot reuse node "
                        f"{flow_id}: a complete valid runtime artifact is required"
                    )
                artifact = artifact.model_copy(deep=True)
                step = PipelineGenerationRunStep(
                    flow_id=flow_id,
                    status="running",
                    stage="replaying",
                    attempts=0,
                    inputs=available_inputs,
                    validation_report=artifact.validation_report,
                )
                run.steps.append(step)
                await emit_pipeline_progress(progress_callback, run)
                if request.options.validation_mode in {
                    "unit",
                    "edge",
                    "pipeline_sample",
                }:
                    loop = asyncio.get_running_loop()

                    async def update_replay_stage(
                        stage: str,
                        current_step: PipelineGenerationRunStep = step,
                    ) -> None:
                        current_step.stage = stage
                        await emit_pipeline_progress(progress_callback, run)

                    execution = await asyncio.to_thread(
                        execute_node_with_docker_handoff,
                        flow_id=flow_id,
                        artifact=artifact,
                        input_files=available_inputs,
                        handoff_dir=handoff_root / flow_id,
                        timeout_seconds=request.context.runtime_constraints.max_runtime_seconds,
                        run_id=run.run_id,
                        stage_callback=threadsafe_stage_callback(
                            loop,
                            update_replay_stage,
                        ),
                    )
                    merge_validation_report(
                        artifact.validation_report,
                        execution.validation_report,
                    )
                    artifact.files = files_with_validation_report(
                        artifact.files,
                        artifact.validation_report,
                    )
                    step.validation_report = artifact.validation_report
                    step.outputs = execution.outputs
                step.status = (
                    "valid"
                    if artifact.validation_report.status == "valid"
                    else "invalid"
                )
                step.stage = (
                    "reused_validated_bundle"
                    if step.status == "valid"
                    else "failed"
                )
                generated_nodes.append(
                    PipelineGeneratedNode(
                        flow_id=flow_id,
                        generated_artifact=artifact,
                    )
                )
                outputs_by_node[flow_id] = artifact.data_contract.outputs
                if step.outputs:
                    handoff_outputs_by_node[flow_id] = step.outputs
                await emit_pipeline_progress(progress_callback, run)
                if step.status == "invalid":
                    append_skipped_steps(run, ordered_ids, nodes_by_id, flow_id)
                    await emit_pipeline_progress(progress_callback, run)
                    break
                continue

            step = PipelineGenerationRunStep(
                flow_id=flow_id,
                status="running",
                stage="generating",
                inputs=available_inputs,
            )
            run.steps.append(step)
            await emit_pipeline_progress(progress_callback, run)
            node_response = await generate_node_script_bundle(
                GenerateNodeScriptRequest(
                    context=context,
                    # Preserve the requested validation level here so runtime
                    # failures participate in the node generator's repair loop.
                    # The following handoff execution still materializes the
                    # validated artifact for downstream nodes.
                    options=request.options,
                    llm_config=request.llm_config,
                ),
                usage_callback=record_usage,
            )
            artifact = node_response.generated_artifact
            step.stage = "static_validation"
            step.validation_report = artifact.validation_report
            await emit_pipeline_progress(progress_callback, run)

            if (
                artifact.validation_report.status == "valid"
                and request.options.validation_mode
                in {
                    "unit",
                    "edge",
                    "pipeline_sample",
                }
            ):
                step.stage = "dependency_validation"
                await emit_pipeline_progress(progress_callback, run)
                loop = asyncio.get_running_loop()

                async def update_node_stage(
                    stage: str,
                    current_step: PipelineGenerationRunStep = step,
                ) -> None:
                    current_step.stage = stage
                    await emit_pipeline_progress(progress_callback, run)

                execution = await asyncio.to_thread(
                    execute_node_with_docker_handoff,
                    flow_id=flow_id,
                    artifact=artifact,
                    input_files=available_inputs,
                    handoff_dir=handoff_root / flow_id,
                    timeout_seconds=request.context.runtime_constraints.max_runtime_seconds,
                    run_id=run.run_id,
                    stage_callback=threadsafe_stage_callback(
                        loop,
                        update_node_stage,
                    ),
                )
                merge_validation_report(
                    artifact.validation_report,
                    execution.validation_report,
                )
                artifact.files = files_with_validation_report(
                    artifact.files,
                    artifact.validation_report,
                )
                step.validation_report = artifact.validation_report
                step.outputs = execution.outputs
                await emit_pipeline_progress(progress_callback, run)

            step.status = (
                "valid" if artifact.validation_report.status == "valid" else "invalid"
            )
            step.stage = "complete" if step.status == "valid" else "failed"
            generated_nodes.append(
                PipelineGeneratedNode(
                    flow_id=flow_id,
                    generated_artifact=artifact,
                )
            )
            outputs_by_node[flow_id] = artifact.data_contract.outputs
            if step.outputs:
                handoff_outputs_by_node[flow_id] = step.outputs
            await emit_pipeline_progress(progress_callback, run)

            if step.status == "invalid":
                append_skipped_steps(run, ordered_ids, nodes_by_id, flow_id)
                await emit_pipeline_progress(progress_callback, run)
                break

    integration_validation = pipeline_contract_validation(
        generated_nodes,
        request.options.validation_mode,
    )
    for step in run.steps:
        if step.validation_report is not None:
            merge_validation_report(integration_validation, step.validation_report)
    run.status = "invalid" if integration_validation.status == "invalid" else "valid"
    run.current_stage = "complete"
    run.stage_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    run.errors = list(integration_validation.errors)
    run.warnings = list(integration_validation.warnings)
    await emit_pipeline_progress(progress_callback, run)
    edge_contracts = build_edge_contracts(request.context.graph, generated_nodes)
    return GeneratePipelineScriptsResponse(
        nodes=generated_nodes,
        edges=edge_contracts,
        integration_validation=integration_validation,
        generation_run=run,
    )


async def run_pipeline_docker_validation(
    pipeline_flow_ids: list[str],
    artifacts_by_node: dict[str, GeneratedArtifact],
    contexts_by_node: dict[str, GenerationContext],
    timeout_seconds: int,
) -> ValidationReport:
    return await asyncio.to_thread(
        validate_pipeline_with_docker,
        ordered_flow_ids=pipeline_flow_ids,
        artifacts_by_node=artifacts_by_node,
        root_inputs_by_node={
            flow_id: context.target_node.files
            for flow_id, context in contexts_by_node.items()
        },
        timeout_seconds=timeout_seconds,
    )


def pipeline_contract_validation(
    generated_nodes: list[PipelineGeneratedNode],
    validation_mode: str,
) -> ValidationReport:
    invalid_nodes = [
        item.flow_id
        for item in generated_nodes
        if item.generated_artifact.validation_report.status != "valid"
    ]
    return ValidationReport(
        status="invalid" if invalid_nodes else "valid",
        checks=[
            "graph_topological_order",
            "node_contract_generation",
            "edge_contract_propagation",
        ],
        errors=[
            f"Node {flow_id} did not produce a valid generated artifact."
            for flow_id in invalid_nodes
        ],
        warnings=(
            []
            if validation_mode in {"edge", "pipeline_sample"}
            else ["Pipeline-level execution validation is not enabled."]
        ),
    )


def first_failed_flow_id(errors: list[str]) -> str | None:
    for error in errors:
        match = re.search(r"\bNode\s+([^:\s]+):", error)
        if match:
            return match.group(1)
        match = re.search(r"\bNode\s+([^:\s]+)\s+did not produce", error)
        if match:
            return match.group(1)
    return None


def replace_generated_node(
    generated_nodes: list[PipelineGeneratedNode],
    flow_id: str,
    artifact: GeneratedArtifact,
) -> None:
    for index, item in enumerate(generated_nodes):
        if item.flow_id == flow_id:
            generated_nodes[index] = PipelineGeneratedNode(
                flow_id=flow_id,
                generated_artifact=artifact,
            )
            return


def files_with_validation_report(
    files: list[GeneratedFile],
    validation: ValidationReport,
) -> list[GeneratedFile]:
    return [
        *[item for item in files if item.filename != "validation-report.json"],
        GeneratedFile(
            filename="validation-report.json",
            content=validation.model_dump_json(indent=2) + "\n",
            content_type="application/json",
        ),
    ]


def append_skipped_steps(
    run: PipelineGenerationRun,
    ordered_ids: list[str],
    nodes_by_id: dict[str, Any],
    failed_flow_id: str,
) -> None:
    try:
        start_index = ordered_ids.index(failed_flow_id) + 1
    except ValueError:
        return
    for flow_id in ordered_ids[start_index:]:
        node = nodes_by_id.get(flow_id)
        if node is None or node.type == "config":
            continue
        run.steps.append(
            PipelineGenerationRunStep(
                flow_id=flow_id,
                status="skipped",
                stage=f"blocked_by_{failed_flow_id}",
            )
        )


def build_edge_contracts(
    graph: GraphContext,
    generated_nodes: list[PipelineGeneratedNode],
) -> list[EdgeDataContract]:
    outputs_by_node = {
        item.flow_id: item.generated_artifact.data_contract.outputs
        for item in generated_nodes
    }
    return [
        EdgeDataContract(
            source=edge.source,
            target=edge.target,
            outputs=outputs_by_node.get(edge.source, []),
        )
        for edge in graph.edges
        if edge.source in outputs_by_node
    ]


def files_from_payload(
    request: GenerateNodeScriptRequest,
    payload: dict[str, Any],
) -> list[GeneratedFile]:
    main_py = str(payload.get("main_py") or "").strip()
    if not main_py:
        main_py = fallback_script_payload(request)["main_py"]

    requirements = normalize_requirements(
        payload.get("requirements"),
        merge_requirements(
            request.context.runtime_constraints.allowed_packages,
            required_packages_for_node(
                request.context.target_node,
                request.context.available_inputs,
            ),
        ),
        main_py=main_py,
        required_packages=required_packages_for_node(
            request.context.target_node,
            request.context.available_inputs,
        ),
    )
    requirements_txt = "\n".join(requirements)
    if requirements_txt:
        requirements_txt += "\n"

    manifest = node_manifest(request, payload, requirements)
    return [
        GeneratedFile(
            filename="main.py", content=main_py + "\n", content_type="text/x-python"
        ),
        GeneratedFile(filename="requirements.txt", content=requirements_txt),
        GeneratedFile(
            filename="node-manifest.json",
            content=json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            content_type="application/json",
        ),
    ]


def normalize_requirements(
    raw: Any,
    allowed_packages: list[str],
    *,
    main_py: str = "",
    required_packages: list[str] | None = None,
) -> list[str]:
    if not isinstance(raw, list):
        raw = []
    allowed_by_name = {
        package_name(item): str(item).strip()
        for item in allowed_packages
        if package_name(item)
    }
    requirements: list[str] = []
    seen: set[str] = set()
    compiler_required = list(required_packages or [])
    for index, item in enumerate([*compiler_required, *raw]):
        # Requirements produced by the planner/compiler are trusted. Model
        # output is still constrained to strings and the request allowlist.
        if index >= len(compiler_required) and not isinstance(item, str):
            continue
        text = str(item or "").strip()
        name = package_name(text)
        if not name or name in seen:
            continue
        if allowed_by_name and name not in allowed_by_name:
            continue
        requirements.append(allowed_by_name.get(name, text))
        seen.add(name)
    for inferred in infer_requirements_from_imports(main_py, allowed_by_name):
        name = package_name(inferred)
        if name and name not in seen:
            requirements.append(inferred)
            seen.add(name)
    return requirements


def merge_requirements(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            text = str(item or "").strip()
            name = package_name(text)
            if name and name not in seen:
                merged.append(text)
                seen.add(name)
    return merged


def requirements_for_compiled_source(
    raw: Any,
    allowed_packages: list[str],
    *,
    source: str,
    required_packages: list[str],
) -> list[str]:
    imported = {
        requirement_name_for_import(root) for root in imported_roots_from_python(source)
    }
    required_names = {
        package_name(item) for item in required_packages if package_name(item)
    }
    raw_items = raw if isinstance(raw, list) else []
    relevant_raw = [
        str(item)
        for item in raw_items
        if package_name(str(item)) in imported | required_names
    ]
    return normalize_requirements(
        relevant_raw,
        allowed_packages,
        main_py=source,
        required_packages=required_packages,
    )


def infer_requirements_from_imports(
    main_py: str,
    allowed_by_name: dict[str, str],
) -> list[str]:
    if not main_py or not allowed_by_name:
        return []
    requirements: list[str] = []
    for import_root in sorted(imported_roots_from_python(main_py)):
        name = requirement_name_for_import(import_root)
        if name and name in allowed_by_name:
            requirements.append(allowed_by_name[name])
    return requirements


def data_contract_from_payload(
    request: GenerateNodeScriptRequest,
    payload: dict[str, Any],
) -> DataContract:
    output_payload = payload.get("outputs")
    if request.context.expected_outputs:
        outputs = request.context.expected_outputs
    elif isinstance(output_payload, list):
        outputs = [
            ExpectedArtifact.model_validate(item)
            for item in output_payload
            if isinstance(item, dict)
        ]
    else:
        outputs = []
    return DataContract(
        inputs=[
            ExpectedArtifact(
                name=Path(file.filename).stem
                if "/" in file.filename
                else file.filename,
                kind=file.kind or "binary",
                format=file.format,
                filename=file.filename,
                columns=file.columns,
                required_columns=file.required_columns,
                schema=file.schema,
                semantic_role=file.semantic_role,
                description="Available input file",
            )
            for file in request.context.available_inputs
        ],
        outputs=outputs,
    )


def node_manifest(
    request: GenerateNodeScriptRequest,
    payload: dict[str, Any],
    requirements: list[str],
) -> dict[str, Any]:
    flow_id = request.context.target_node.flow_id
    return {
        "schema_version": 1,
        "flow_id": flow_id,
        "generator": "inlumen-codegen-service",
        "generator_version": GENERATOR_VERSION,
        "entrypoint": ["python", "/app/main.py"],
        "runtime": {
            "language": "python",
            "python_version": request.context.runtime_constraints.python_version,
            "base_image": request.context.runtime_constraints.base_image,
            "network_allowed": request.context.runtime_constraints.network_allowed,
            "max_runtime_seconds": request.context.runtime_constraints.max_runtime_seconds,
        },
        "dependencies": requirements,
        "implementation_plan": (
            implementation_plan_for_node(
                request.context.target_node,
                request.context.available_inputs,
            )
            or (
                dict(payload["implementation_plan"])
                if isinstance(payload.get("implementation_plan"), dict)
                else {}
            )
        ),
        "data_contract": data_contract_from_payload(request, payload).model_dump(
            mode="json"
        ),
        "notes": payload.get("notes") if isinstance(payload.get("notes"), list) else [],
    }


def sanitize_fragment(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-")
    return text or "node"


def expected_outputs_for_node(
    node,
    child_ids: list[str],
    available_inputs: list[FileDescriptor] | None = None,
) -> list[ExpectedArtifact]:
    # Destinations are compiler-owned boundary adapters. Their upstream task must
    # produce the business payload; the destination itself only emits a delivery
    # receipt at runtime. Do not turn words such as "report" or "sentiment" in a
    # destination label into a second generated transformation contract.
    if is_output_node_kind(node.type):
        return []
    label = node.label or node.flow_id
    name = sanitize_fragment(label).replace("-", "_").lower() or "output"
    inputs = available_inputs or []
    label_text = f"{node.label} {node.description}".lower()
    table_input = first_table_input(inputs)
    task = classify_node_task(node, inputs)
    task_name = f"final_{name}" if is_output_node_kind(node.type) else name
    if task == "speech_to_text":
        return [
            ExpectedArtifact(
                name=task_name,
                kind="json",
                format="json",
                filename=f"{task_name}.json",
                schema={
                    "type": "object",
                    "required": ["transcript", "segments", "processing_metadata"],
                    "properties": {
                        "transcript": {"type": "string"},
                        "segments": {"type": "array"},
                        "processing_metadata": {"type": "object"},
                    },
                },
                semantic_role="transcription",
                description=f"Speech transcription produced by {label}.",
            )
        ]
    if task == "sentiment":
        return [
            ExpectedArtifact(
                name=task_name,
                kind="json",
                format="json",
                filename=f"{task_name}.json",
                schema={
                    "type": "object",
                    "required": [
                        "transcript",
                        "sentiment_label",
                        "confidence",
                        "sentiment_scores",
                        "processing_metadata",
                    ],
                    "properties": {
                        "transcript": {"type": "string"},
                        "sentiment_label": {
                            "type": "string",
                            "enum": ["positive", "neutral", "negative"],
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "sentiment_scores": {"type": "object"},
                        "processing_metadata": {"type": "object"},
                    },
                },
                semantic_role="sentiment",
                description=f"Sentiment result produced by {label}.",
            )
        ]
    if task == "audio_preprocessing":
        audio_input = next(
            (
                item
                for item in inputs
                if str(item.format or "").lower()
                in {"wav", "wave", "mp3", "m4a", "aac", "flac", "ogg"}
            ),
            None,
        )
        output_format = (
            "wav"
            if "16-bit pcm wav" in label_text or "16 khz" in label_text
            else str(audio_input.format or "wav")
            if audio_input
            else "wav"
        )
        return [
            ExpectedArtifact(
                name=task_name,
                kind="binary",
                format=output_format,
                filename=f"{task_name}.{output_format}",
                semantic_role="prepared_audio",
                description=f"Processed audio produced by {label}.",
            )
        ]
    if task == "report":
        return [
            ExpectedArtifact(
                name=task_name,
                kind="json",
                format="json",
                filename=f"{task_name}.json",
                schema={
                    "type": "object",
                    "required": [
                        "transcription",
                        "sentiment_label",
                        "confidence_score",
                        "processing_metadata",
                    ],
                    "properties": {
                        "transcription": {"type": "string"},
                        "sentiment_label": {"type": "string"},
                        "confidence_score": {"type": "number"},
                        "processing_metadata": {"type": "object"},
                    },
                },
                semantic_role="report",
                description=f"Processing report produced by {label}.",
            )
        ]
    if task == "pdf_extraction":
        return [
            ExpectedArtifact(
                name=task_name,
                kind="json",
                format="json",
                filename=f"{task_name}.json",
                schema={
                    "type": "object",
                    "required": ["text", "pages"],
                    "properties": {
                        "text": {"type": "string"},
                        "pages": {"type": "array"},
                    },
                },
                semantic_role="extracted_document",
                description=f"Structured PDF content produced by {label}.",
            )
        ]
    if task == "document_chunking":
        return [
            ExpectedArtifact(
                name=task_name,
                kind="json",
                format="json",
                filename=f"{task_name}.json",
                schema={
                    "type": "object",
                    "required": ["chunks"],
                    "properties": {
                        "chunks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["id", "text", "page", "source"],
                                "properties": {
                                    "id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "page": {"type": "integer"},
                                    "source": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                semantic_role="document_chunks",
                description=(
                    f"Overlapping document chunks with stable source and page metadata produced by {label}."
                ),
            )
        ]
    if task == "document_indexing":
        role = "embedding_records" if "embedding" in label_text else "vector_index"
        return [
            ExpectedArtifact(
                name=task_name,
                kind="json",
                format="json",
                filename=f"{task_name}.json",
                schema={
                    "type": "object",
                    "required": ["records"],
                    "properties": {
                        "records": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["id", "text", "page", "source", "vector"],
                                "properties": {
                                    "id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "page": {"type": "integer"},
                                    "source": {"type": "string"},
                                    "vector": {"type": "array"},
                                },
                            },
                        },
                        "method": {"type": "string"},
                    },
                },
                semantic_role=role,
                description=f"Local searchable document representation produced by {label}.",
            )
        ]
    if task == "question_answering":
        return [
            ExpectedArtifact(
                name=task_name,
                kind="json",
                format="json",
                filename=f"{task_name}.json",
                schema={
                    "type": "object",
                    "required": ["answers"],
                    "properties": {
                        "answers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["question", "answer", "citations"],
                                "properties": {
                                    "question": {"type": "string"},
                                    "answer": {"type": "string"},
                                    "citations": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": ["source", "page"],
                                            "properties": {
                                                "source": {"type": "string"},
                                                "page": {"type": "integer"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
                semantic_role="grounded_answer",
                description=f"Retrieved answer with source and page citations produced by {label}.",
            )
        ]
    if task == "alerting":
        return [
            ExpectedArtifact(
                name=task_name,
                kind="json",
                format="json",
                filename=f"{task_name}.json",
                schema={
                    "type": "object",
                    "required": ["alerts"],
                    "properties": {
                        "alerts": {"type": "array"},
                    },
                },
                semantic_role="alerts",
                description=f"Explicit patient alerts produced by {label}.",
            )
        ]
    if is_input_node_kind(node.type):
        data_file = next((file for file in node.files if file.kind), None)
        if data_file is not None:
            output_format = data_file.format or extension_for_kind(
                data_file.kind or "binary"
            )
            return [
                ExpectedArtifact(
                    name=f"raw_{name}",
                    kind=data_file.kind or "binary",
                    format=output_format,
                    filename=f"raw_{name}.{output_format}",
                    columns=data_file.columns,
                    required_columns=required_columns_for_data_file(data_file),
                    schema=data_file.schema,
                    semantic_role=(
                        data_file.semantic_role or semantic_role_for_input(data_file)
                    ),
                    description=(
                        f"Raw data from {label}"
                        + (f" for {', '.join(child_ids)}." if child_ids else ".")
                    ),
                )
            ]
        name = f"raw_{name}"
    elif is_output_node_kind(node.type):
        name = f"final_{name}"
    elif any(
        keyword in label_text
        for keyword in ("image", "resize", "crop", "vision", "photo")
    ):
        image_input = next(
            (
                file
                for file in inputs
                if file.kind == "image"
                or file.format in {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
            ),
            None,
        )
        if image_input is not None:
            image_format = image_input.format or "png"
            return [
                ExpectedArtifact(
                    name=name,
                    kind="image",
                    format=image_format,
                    filename=f"{name}.{image_format}",
                    semantic_role="processed_image",
                    description=f"Image produced by {label}.",
                )
            ]
    elif any(
        keyword in label_text
        for keyword in ("audio", "resample", "denoise", "normalize sound")
    ):
        audio_input = next(
            (
                file
                for file in inputs
                if file.format in {"wav", "wave", "mp3", "m4a", "aac", "flac", "ogg"}
            ),
            None,
        )
        if audio_input is not None:
            audio_format = audio_input.format or "wav"
            return [
                ExpectedArtifact(
                    name=name,
                    kind="binary",
                    format=audio_format,
                    filename=f"{name}.{audio_format}",
                    semantic_role="processed_audio",
                    description=f"Audio produced by {label}.",
                )
            ]
    elif any(file.format == "pdf" for file in inputs) and any(
        keyword in label_text for keyword in ("extract", "parse", "document", "pdf")
    ):
        return [
            ExpectedArtifact(
                name=name,
                kind="json",
                format="json",
                filename=f"{name}.json",
                schema={
                    "type": "object",
                    "required": ["text", "pages"],
                    "properties": {
                        "text": {"type": "string"},
                        "pages": {"type": "array"},
                    },
                },
                semantic_role="extracted_document",
                description=f"Structured PDF content produced by {label}.",
            )
        ]
    elif task != "model_training" and any(
        keyword in label_text
        for keyword in ("preprocess", "clean", "normalize", "transform")
    ):
        if table_input is not None:
            return [
                ExpectedArtifact(
                    name=name,
                    kind="table",
                    format=table_input.format or "csv",
                    filename=f"{name}.{table_input.format or 'csv'}",
                    columns=table_input.columns,
                    required_columns=required_columns_for_data_file(table_input),
                    schema=table_input.schema,
                    semantic_role="prepared_dataset",
                    description=f"Cleaned tabular data produced by {label}.",
                )
            ]
    elif "train" in label_text or "model" in label_text:
        target_columns = target_like_columns(table_input.columns if table_input else [])
        model_outputs = [
            ExpectedArtifact(
                name=f"{name}_model",
                kind="model",
                format="pickle",
                filename=f"{name}_model.pickle",
                semantic_role="trained_model",
                description=f"Model artifact produced by {label}.",
            ),
            ExpectedArtifact(
                name=f"{name}_metrics",
                kind="json",
                format="json",
                filename=f"{name}_metrics.json",
                schema={
                    "type": "object",
                    "required": ["metrics", "target_column"],
                    "properties": {
                        "metrics": {"type": "object"},
                        "target_column": {
                            "type": "string",
                            "enum": target_columns,
                        }
                        if target_columns
                        else {"type": "string"},
                    },
                },
                semantic_role="model_metrics",
                description=f"Training metrics produced by {label}.",
            ),
        ]
        if table_input is not None:
            prediction_columns = list(
                dict.fromkeys(
                    [
                        *table_input.columns,
                        "prediction",
                        "prediction_score",
                    ]
                )
            )
            model_outputs.append(
                ExpectedArtifact(
                    name=f"{name}_predictions",
                    kind="table",
                    format=table_input.format or "csv",
                    filename=f"{name}_predictions.{table_input.format or 'csv'}",
                    columns=prediction_columns,
                    required_columns=prediction_columns,
                    semantic_role="model_predictions",
                    description=(
                        f"Row-level predictions and scores produced by {label}; "
                        "source identity columns are preserved for downstream actions."
                    ),
                )
            )
        return model_outputs
    elif any(keyword in label_text for keyword in ("alert", "notify", "warning")):
        return [
            ExpectedArtifact(
                name=name,
                kind="json",
                format="json",
                filename=f"{name}.json",
                schema={
                    "type": "object",
                    "required": ["alerts"],
                    "properties": {
                        "alerts": {"type": "array"},
                    },
                },
                semantic_role="alerts",
                description=f"Alerts generated by {label}.",
            )
        ]
    return [
        ExpectedArtifact(
            name=name,
            kind="json",
            format="json",
            filename=f"{name}.json",
            description=(
                f"Output from {label}"
                + (f" for {', '.join(child_ids)}." if child_ids else ".")
            ),
        )
    ]


def extension_for_kind(kind: str) -> str:
    return {
        "table": "csv",
        "json": "json",
        "text": "txt",
        "image": "png",
        "model": "pickle",
        "directory": "dir",
        "binary": "bin",
    }.get(kind, "bin")


def semantic_role_for_input(file: FileDescriptor) -> str:
    if file.kind == "image":
        return "source_image"
    if file.format in {"wav", "wave", "mp3", "m4a", "aac", "flac", "ogg"}:
        return "source_audio"
    if file.format == "pdf":
        return "source_document"
    if file.kind == "table":
        return "raw_dataset"
    return "source_artifact"


def validate_contract_alignment(
    *,
    expected_outputs: list[ExpectedArtifact],
    actual: DataContract,
) -> ValidationReport:
    checks = ["planned_output_contract_alignment"]
    errors: list[str] = []
    actual_by_name = {item.name: item for item in actual.outputs}
    for expected in expected_outputs:
        actual_output = actual_by_name.get(expected.name)
        if actual_output is None:
            errors.append(f"Missing planned output contract: {expected.name}")
            continue
        if actual_output.kind != expected.kind:
            errors.append(
                f"Output {expected.name} kind mismatch: "
                f"expected {expected.kind}, got {actual_output.kind}"
            )
        if expected.format and actual_output.format != expected.format:
            errors.append(
                f"Output {expected.name} format mismatch: "
                f"expected {expected.format}, got {actual_output.format}"
            )
    return ValidationReport(
        status="invalid" if errors else "valid",
        checks=checks,
        errors=errors,
    )


def first_table_input(inputs: list[FileDescriptor]) -> FileDescriptor | None:
    return next(
        (
            file
            for file in inputs
            if file.kind == "table"
            and (file.format in {"csv", "tsv"} or not file.format)
        ),
        None,
    )


def required_columns_for_data_file(file: FileDescriptor) -> list[str]:
    required = list(file.required_columns)
    for column in target_like_columns(file.columns):
        if column not in required:
            required.append(column)
    return required


def target_like_columns(columns: list[str]) -> list[str]:
    target_tokens = (
        "target",
        "label",
        "class",
        "outcome",
        "condition",
        "diagnosis",
        "status",
        "alert",
    )
    return [
        column
        for column in columns
        if any(token in column.lower() for token in target_tokens)
    ]


def merge_validation_report(target: ValidationReport, extra: ValidationReport) -> None:
    target.checks.extend(check for check in extra.checks if check not in target.checks)
    target.errors.extend(extra.errors)
    target.warnings.extend(extra.warnings)
    if target.errors:
        target.status = "invalid"
    elif extra.status == "not_run" and target.status == "valid":
        target.warnings.append("Requested validation step was not run.")


def artifact_filename(flow_id: str, artifact: ExpectedArtifact) -> str:
    extension = sanitize_fragment(artifact.format or artifact.kind)
    name = sanitize_fragment(artifact.name).replace("-", "_").lower()
    return f"{sanitize_fragment(flow_id)}/{name}.{extension}"


def topological_order(graph: GraphContext) -> list[str]:
    ids = [node.flow_id for node in graph.nodes if node.flow_id]
    id_set = set(ids)
    incoming = {flow_id: 0 for flow_id in ids}
    outgoing: dict[str, list[str]] = {flow_id: [] for flow_id in ids}
    for edge in graph.edges:
        if edge.source not in id_set or edge.target not in id_set:
            continue
        outgoing[edge.source].append(edge.target)
        incoming[edge.target] += 1
    ready = sorted([flow_id for flow_id, count in incoming.items() if count == 0])
    ordered: list[str] = []
    while ready:
        flow_id = ready.pop(0)
        ordered.append(flow_id)
        for target in sorted(outgoing.get(flow_id, [])):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if len(ordered) != len(ids):
        raise ValueError("Pipeline graph contains a cycle.")
    return ordered


def fallback_script_payload(request: GenerateNodeScriptRequest) -> dict[str, Any]:
    expected_outputs = request.context.expected_outputs or [
        ExpectedArtifact(name="output", kind="json", format="json")
    ]
    output_specs = [
        {
            "name": item.name,
            "kind": item.kind,
            "format": item.format or "json",
            "description": item.description,
            "filename": item.filename,
            "columns": item.columns,
            "required_columns": item.required_columns,
            "schema": item.schema,
            "semantic_role": item.semantic_role,
        }
        for item in expected_outputs
    ]
    output_specs_json = json.dumps(output_specs, indent=4)
    main_py = f'''"""Generated InLumen node script.

This deterministic runtime implements the reviewed inLumen task profile and
the generic pipeline handoff contract without an LLM dependency.
"""

from __future__ import annotations

import json
import os
import pickle
import shutil
import csv
from pathlib import Path


def load_json(path: str) -> dict:
    if not path:
        return {{}}
    candidate = Path(path)
    if not candidate.exists():
        return {{}}
    with candidate.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {{}}


def manifest_entries(manifest: dict) -> list[dict]:
    entries = manifest.get("inputs") or manifest.get("files") or []
    return entries if isinstance(entries, list) else []


def source_path_for(entry: dict, manifest_path: str) -> Path:
    raw_path = entry.get("path") or entry.get("filename") or ""
    path = Path(str(raw_path))
    if path.is_absolute() and path.exists():
        return path
    return Path(manifest_path).parent / str(entry.get("filename") or raw_path)


def default_json_value(schema: dict | None):
    if not isinstance(schema, dict):
        return None
    schema_type = schema.get("type")
    if schema_type == "array":
        return []
    if schema_type == "object":
        return {{}}
    if schema_type in {{"integer", "number"}}:
        return 0
    if schema_type == "boolean":
        return False
    return ""


def target_column_for_spec(spec: dict, input_entries: list[dict]) -> str:
    schema = spec.get("schema") if isinstance(spec.get("schema"), dict) else {{}}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {{}}
    target_schema = (
        properties.get("target_column")
        if isinstance(properties.get("target_column"), dict)
        else {{}}
    )
    enum = target_schema.get("enum") if isinstance(target_schema.get("enum"), list) else []
    if enum:
        return str(enum[0])
    for entry in input_entries:
        for column in entry.get("required_columns") or []:
            if column:
                return str(column)
    for entry in input_entries:
        for column in entry.get("columns") or []:
            text = str(column)
            if any(token in text.lower() for token in ("target", "label", "condition", "status")):
                return text
    return ""


def sample_row_count(input_entries: list[dict]) -> int:
    for entry in input_entries:
        row_count = entry.get("row_count")
        if isinstance(row_count, int):
            return row_count
    return 0


def json_payload_for_spec(spec: dict, fallback_payload: dict, input_entries: list[dict]):
    schema = spec.get("schema") if isinstance(spec.get("schema"), dict) else {{}}
    required = [str(item) for item in schema.get("required", []) if str(item)]
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {{}}
    semantic_role = str(spec.get("semantic_role") or "")

    if semantic_role == "model_metrics" or "metrics" in required:
        return {{
            "metrics": {{
                "status": "fallback",
                "row_count": sample_row_count(input_entries),
            }},
            "target_column": target_column_for_spec(spec, input_entries),
        }}
    if semantic_role == "alerts" or "alerts" in required:
        return {{
            "alerts": [],
            "status": "fallback",
        }}
    if schema.get("type") == "array":
        return []
    if schema.get("type") == "object":
        result = {{
            "message": "Generated fallback output",
            "input_manifest": fallback_payload.get("input_manifest", {{}}),
            "context": fallback_payload.get("context", {{}}),
        }}
        for key in required:
            if key not in result:
                property_schema = (
                    properties.get(key)
                    if isinstance(properties.get(key), dict)
                    else {{}}
                )
                result[key] = default_json_value(property_schema)
        return result
    return fallback_payload


def main() -> None:
    input_manifest_path = os.getenv("INLUMEN_INPUT_MANIFEST", "")
    output_dir = Path(os.getenv("INLUMEN_OUTPUT_DIR", "/inlumen/outputs"))
    output_manifest_path = Path(
        os.getenv("INLUMEN_OUTPUT_MANIFEST", str(output_dir / "output_manifest.json"))
    )
    context_path = os.getenv("INLUMEN_CONTEXT_PATH", "")

    output_dir.mkdir(parents=True, exist_ok=True)
    input_manifest = load_json(input_manifest_path)
    context = load_json(context_path)

    output_specs = {output_specs_json}
    manifest_outputs = []
    payload = {{
        "message": "Generated fallback output",
        "input_manifest": input_manifest,
        "context": context,
    }}
    input_entries = manifest_entries(input_manifest)
    for spec in output_specs:
        name = spec.get("name") or "output"
        file_format = spec.get("format") or "json"
        output_path = output_dir / (spec.get("filename") or f"{{name}}.{{file_format}}")
        matching_input = next(
            (
                entry
                for entry in input_entries
                if entry.get("kind") == spec.get("kind")
                and (not spec.get("format") or entry.get("format") == spec.get("format"))
            ),
            None,
        )
        if matching_input and spec.get("kind") == "table" and file_format in {{"csv", "tsv"}}:
            delimiter = "\\t" if file_format == "tsv" else ","
            source_path = source_path_for(matching_input, input_manifest_path)
            with source_path.open("r", encoding="utf-8", newline="") as source_handle:
                reader = csv.DictReader(source_handle, delimiter=delimiter)
                rows = list(reader)
                columns = spec.get("columns") or reader.fieldnames or []
            if not columns:
                columns = ["value"]
            with output_path.open("w", encoding="utf-8", newline="") as output_handle:
                writer = csv.DictWriter(
                    output_handle,
                    fieldnames=columns,
                    delimiter=delimiter,
                    extrasaction="ignore",
                )
                writer.writeheader()
                for row in rows or [{{column: "" for column in columns}}]:
                    writer.writerow({{column: row.get(column, "") for column in columns}})
        elif matching_input and spec.get("kind") in {{
            "table", "text", "image", "audio", "video", "document", "binary"
        }}:
            shutil.copy2(source_path_for(matching_input, input_manifest_path), output_path)
        elif file_format in {{"pickle", "pkl"}}:
            with output_path.open("wb") as handle:
                pickle.dump(payload, handle)
        else:
            with output_path.open("w", encoding="utf-8") as handle:
                json.dump(
                    json_payload_for_spec(spec, payload, input_entries),
                    handle,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\\n")
        manifest_outputs.append(
            {{
                "name": name,
                "kind": spec.get("kind") or "json",
                "format": file_format,
                "filename": output_path.name,
                "path": str(output_path),
                "columns": spec.get("columns") or [],
                "required_columns": spec.get("required_columns") or [],
                "schema": spec.get("schema") or {{}},
                "semantic_role": spec.get("semantic_role") or "",
            }}
        )

    output_manifest = {{
        "schema_version": "inlumen.output-manifest@1",
        "flow_id": os.getenv("INLUMEN_FLOW_ID", "{request.context.target_node.flow_id}"),
        "outputs": manifest_outputs,
    }}
    output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with output_manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(output_manifest, handle, indent=2, sort_keys=True)
        handle.write("\\n")


if __name__ == "__main__":
    main()
'''
    return {
        "main_py": main_py,
        "requirements": [],
        "outputs": output_specs,
        "notes": ["Deterministic fallback used for endpoint smoke testing."],
    }
