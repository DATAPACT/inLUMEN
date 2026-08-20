CANONICAL_STEP_TYPES = {
    "source",
    "task",
    "destination",
    "flow",
    "subpipeline",
}

# The graph contract deliberately stays small. Legacy types, UI vocabulary,
# and domain operations normalize to one of the five structural kinds.
STEP_TYPE_ALIASES = {
    "input": "source",
    "data_ingestion": "source",
    "data-source": "source",
    "data_source": "source",
    "ingest": "source",
    "ingestion": "source",
    "sensor": "source",
    "sensors": "source",
    "collect": "source",
    "collection": "source",
    "sink": "destination",
    "output": "destination",
    "alert": "destination",
    "alerting": "destination",
    "notification": "destination",
    "notify": "destination",
    "report": "destination",
    "reporting": "destination",
    "dashboard": "destination",
    "result": "destination",
    "results": "destination",
    "action": "task",
    "config": "task",
    "configuration": "task",
    "custom": "task",
    "api": "task",
    "api_call": "task",
    "api-call": "task",
    "endpoint": "task",
    "integration": "task",
    "service": "task",
    "external_service": "task",
    "external-service": "task",
    "storage": "task",
    "database": "task",
    "db": "task",
    "clipboard": "task",
    "data_store": "task",
    "data-store": "task",
    "persistence": "task",
    "parameter": "task",
    "parameters": "task",
    "settings": "task",
    "process": "task",
    "processing": "task",
    "processing_step": "task",
    "operation": "task",
    "operator": "task",
    "step": "task",
    "preprocess": "task",
    "preprocessing": "task",
    "transform": "task",
    "transformation": "task",
    "feature_engineering": "task",
    "feature-engineering": "task",
    "training": "task",
    "model_training": "task",
    "model-training": "task",
    "evaluation": "task",
    "model_evaluation": "task",
    "model-evaluation": "task",
    "inference": "task",
    "scoring": "task",
    "control": "flow",
    "condition": "flow",
    "branch": "flow",
    "parallel": "flow",
    "parallel_map": "flow",
    "parallel-map": "flow",
    "merge": "flow",
    "retry": "flow",
    "wait": "flow",
    "human_approval": "flow",
    "human-approval": "flow",
    "sub_pipeline": "subpipeline",
    "sub-pipeline": "subpipeline",
    "nested_pipeline": "subpipeline",
    "nested-pipeline": "subpipeline",
    "reusable_pipeline": "subpipeline",
    "reusable-pipeline": "subpipeline",
}


def normalize_step_type(raw_type: object, default: str = "task") -> str:
    normalized = str(raw_type or "").strip().lower().replace(" ", "_")
    if normalized in CANONICAL_STEP_TYPES:
        return normalized
    if normalized in STEP_TYPE_ALIASES:
        return STEP_TYPE_ALIASES[normalized]
    if (
        "subpipeline" in normalized
        or "sub_pipeline" in normalized
        or "nested_pipeline" in normalized
    ):
        return "subpipeline"
    if any(token in normalized for token in ("condition", "branch", "parallel", "merge", "retry", "wait", "approval")):
        return "flow"
    if any(token in normalized for token in ("ingest", "input", "source", "sensor", "upload")):
        return "source"
    if any(token in normalized for token in ("sink", "destination", "alert", "output", "report", "publish", "notification")):
        return "destination"
    return default if default in CANONICAL_STEP_TYPES else "task"
