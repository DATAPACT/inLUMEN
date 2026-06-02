CANONICAL_STEP_TYPES = {
    "action",
    "input",
    "output",
    "config",
    "storage",
    "api",
    "custom",
}

STEP_TYPE_ALIASES = {
    "data_ingestion": "input",
    "data-source": "input",
    "data_source": "input",
    "ingest": "input",
    "ingestion": "input",
    "source": "input",
    "sensor": "input",
    "sensors": "input",
    "collect": "input",
    "collection": "input",
    "preprocess": "action",
    "preprocessing": "action",
    "processing": "action",
    "transform": "action",
    "transformation": "action",
    "feature_engineering": "action",
    "feature-engineering": "action",
    "training": "action",
    "model_training": "action",
    "model-training": "action",
    "evaluation": "action",
    "model_evaluation": "action",
    "model-evaluation": "action",
    "inference": "action",
    "scoring": "action",
    "alert": "output",
    "alerting": "output",
    "notification": "output",
    "notify": "output",
    "report": "output",
    "reporting": "output",
    "dashboard": "output",
    "result": "output",
    "results": "output",
    "database": "storage",
    "db": "storage",
    "clipboard": "storage",
    "endpoint": "api",
    "api_call": "api",
    "api-call": "api",
    "model_config": "config",
    "model-config": "config",
    "configuration": "config",
}


def normalize_step_type(raw_type: object, default: str = "action") -> str:
    normalized = str(raw_type or "").strip().lower().replace(" ", "_")
    if normalized in CANONICAL_STEP_TYPES:
        return normalized
    if normalized in STEP_TYPE_ALIASES:
        return STEP_TYPE_ALIASES[normalized]
    if "ingest" in normalized or "input" in normalized or "source" in normalized:
        return "input"
    if "alert" in normalized or "output" in normalized or "report" in normalized:
        return "output"
    if "storage" in normalized or "database" in normalized or "clipboard" in normalized:
        return "storage"
    if "api" in normalized or "endpoint" in normalized:
        return "api"
    if "config" in normalized:
        return "config"
    return default if default in CANONICAL_STEP_TYPES else "action"
