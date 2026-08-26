from __future__ import annotations

import ast
from typing import Any

IMPLEMENTATION_PLAN_SCHEMA = "inlumen.implementation-plan@1"
QUALITY_POLICY_SCHEMA = "inlumen.quality-policy@1"
MODEL_ARTIFACT_POLICY_SCHEMA = "inlumen.model-artifact-policy@1"

LOCAL_MODEL_ARTIFACT_POLICY: dict[str, Any] = {
    "schema_version": MODEL_ARTIFACT_POLICY_SCHEMA,
    "acquisition": "deployment-preflight",
    "source": "huggingface",
    "runtime_access": "verified-local-only",
    "model_root_env": "INLUMEN_MODEL_ROOT",
    "integrity": "sha256-tree",
    "runtime_network": "disabled",
}

FASTER_WHISPER_PLAN: dict[str, Any] = {
    "schema_version": IMPLEMENTATION_PLAN_SCHEMA,
    "execution_profile": "trusted_heavy_model",
    "resource_class": "gpu_preferred",
    "task": "automatic-speech-recognition",
    "domain": "audio",
    "adapter_id": "faster-whisper",
    "adapter_version": "1",
    "framework": "ctranslate2",
    "model_id": "Systran/faster-whisper-large-v3",
    "model_revision": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
    "model_variants": {
        "accuracy": {
            "model_id": "Systran/faster-whisper-large-v3",
            "model_revision": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
        },
        "balanced": {
            "model_id": "Systran/faster-whisper-medium",
            "model_revision": "08e178d48790749d25932bbc082711ddcfdfbc4f",
        },
        "fast": {
            "model_id": "Systran/faster-whisper-small",
            "model_revision": "536b0662742c02347bc0e980a01041f333bce120",
        },
    },
    "runtime_selection": {
        "default_profile": "auto",
        "auto_profile_by_device": {
            "cpu": "balanced",
            "cuda": "accuracy",
        },
    },
    "device": "auto",
    "precision": "auto",
    "artifact_policy": LOCAL_MODEL_ARTIFACT_POLICY,
    "required_packages": [
        "faster-whisper==1.2.1",
        "ctranslate2==4.8.1",
        "huggingface-hub==1.25.1",
    ],
    "inference_parameters": {
        "beam_size": 5,
        "word_timestamps": True,
        "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 500},
        "condition_on_previous_text": True,
        # Leave capacity for the orchestrator and downstream services when ASR
        # runs in the same Docker Compose project. This does not change model
        # accuracy and can be overridden with INLUMEN_ASR_CPU_THREADS.
        "cpu_threads": 2,
        "num_workers": 1,
    },
    "quality_policy": {
        "schema_version": QUALITY_POLICY_SCHEMA,
        "fail_on_empty_transcript": True,
        "min_language_probability": 0.35,
        "min_average_log_probability": -1.5,
        "max_no_speech_probability": 0.85,
        "max_reference_wer": 0.35,
        "max_reference_cer": 0.25,
        "low_confidence_action": "warn",
    },
    "resolution": {
        "source": "inlumen-trusted-adapter-registry",
        "status": "resolved",
    },
}


def infer_implementation_plan_from_python_source(
    source: Any,
    *,
    parameters: Any = None,
) -> dict[str, Any]:
    """Infer a reviewed local-model plan from an uploaded Python module.

    This is deliberately conservative: we only infer models when the library,
    constructor and literal model identifier are all unambiguous.  Unknown or
    dynamically selected models remain user-managed rather than being guessed.
    """
    if not isinstance(source, str):
        return {}
    try:
        tree = ast.parse(source, filename="main.py")
    except SyntaxError:
        return {}
    configured_parameters = parameters if isinstance(parameters, dict) else {}

    for detector in _MODEL_SOURCE_PLAN_DETECTORS:
        plan = detector(tree, configured_parameters)
        if plan:
            return plan
    return {}


def unresolved_model_plan_errors_from_python_source(
    source: Any,
    *,
    parameters: Any = None,
) -> list[str]:
    """Identify uploaded model code that remains user-managed.

    A recognised constructor without a reviewed, pinned revision cannot be
    prefetched by the platform.  The caller reports this as an operational
    warning: arbitrary Task code remains valid and is responsible for making
    its own model available at runtime.
    """
    if not isinstance(source, str):
        return []
    try:
        tree = ast.parse(source, filename="main.py")
    except SyntaxError:
        return []
    if infer_implementation_plan_from_python_source(source, parameters=parameters):
        return []

    direct_whisper: set[str] = set()
    whisper_modules: set[str] = set()
    pipeline_names: set[str] = set()
    transformers_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "faster_whisper":
                direct_whisper.update(
                    imported.asname or imported.name
                    for imported in node.names
                    if imported.name == "WhisperModel"
                )
            elif node.module == "transformers":
                pipeline_names.update(
                    imported.asname or imported.name
                    for imported in node.names
                    if imported.name == "pipeline"
                )
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "faster_whisper":
                    whisper_modules.add(imported.asname or imported.name)
                elif imported.name == "transformers":
                    transformers_modules.add(imported.asname or imported.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_whisper = (
            isinstance(node.func, ast.Name) and node.func.id in direct_whisper
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "WhisperModel"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in whisper_modules
        )
        is_transformers_pipeline = _is_transformers_pipeline(
            node.func,
            pipeline_names,
            transformers_modules,
        )
        if is_whisper or is_transformers_pipeline:
            library = "faster-whisper" if is_whisper else "Transformers pipeline"
            return [
                f"Uploaded {library} code has no reviewed, pinned local model "
                "plan and will remain user-managed at runtime."
            ]
    return []


def _infer_faster_whisper_plan(
    tree: ast.AST,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Resolve literal ``faster_whisper.WhisperModel`` uses from source."""

    direct_names: set[str] = set()
    module_names: set[str] = set()
    string_defaults = _string_default_bindings(tree, parameters)
    cli_defaults = _cli_argument_defaults(tree, parameters)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "faster_whisper":
            for imported in node.names:
                if imported.name == "WhisperModel":
                    direct_names.add(imported.asname or imported.name)
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "faster_whisper":
                    module_names.add(imported.asname or imported.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_whisper_constructor = (
            isinstance(node.func, ast.Name) and node.func.id in direct_names
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "WhisperModel"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_names
        )
        if not is_whisper_constructor:
            continue
        model_literal = _literal_whisper_model_argument(
            node,
            string_defaults,
            cli_defaults,
            parameters,
        )
        if model_literal:
            return _faster_whisper_plan_for_literal(model_literal)
    return {}


def _string_default_bindings(
    tree: ast.AST,
    parameters: dict[str, Any],
) -> dict[str, str]:
    """Find simple string defaults, including ``params.get(name, default)``."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        default = _string_default_from_expression(value, bindings, parameters)
        if not default:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = default
    return bindings


def _cli_argument_defaults(tree: ast.AST, parameters: dict[str, Any]) -> dict[str, str]:
    """Resolve defaults declared with conventional argparse ``--name`` flags."""
    defaults: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        option = next(
            (
                argument.value[2:]
                for argument in node.args
                if isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and argument.value.startswith("--")
            ),
            "",
        )
        if not option:
            continue
        configured = parameters.get(option.replace("-", "_"))
        if isinstance(configured, str) and configured.strip():
            defaults[option.replace("-", "_")] = configured.strip()
            continue
        for keyword in node.keywords:
            if keyword.arg == "default":
                value = _string_default_from_expression(
                    keyword.value,
                    {},
                    parameters,
                )
                if value:
                    defaults[option.replace("-", "_")] = value
    return defaults


def _string_default_from_expression(
    expression: ast.expr,
    bindings: dict[str, str],
    parameters: dict[str, Any],
) -> str:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value.strip()
    if isinstance(expression, ast.Name):
        return bindings.get(expression.id, "")
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "str"
        and expression.args
    ):
        return _string_default_from_expression(expression.args[0], bindings, parameters)
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr in {"get", "getenv"}
        and len(expression.args) >= 2
    ):
        parameter_name = _string_default_from_expression(
            expression.args[0],
            bindings,
            parameters,
        )
        configured = parameters.get(parameter_name)
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return _string_default_from_expression(expression.args[1], bindings, parameters)
    return ""


def _literal_whisper_model_argument(
    call: ast.Call,
    bindings: dict[str, str],
    cli_defaults: dict[str, str],
    parameters: dict[str, Any],
) -> str:
    if call.args:
        if (
            isinstance(call.args[0], ast.Attribute)
            and isinstance(call.args[0].value, ast.Name)
            and call.args[0].value.id == "args"
        ):
            value = cli_defaults.get(call.args[0].attr, "")
        else:
            value = _string_default_from_expression(
                call.args[0],
                bindings,
                parameters,
            )
        if value:
            return value
    for keyword in call.keywords:
        if keyword.arg in {"model_size_or_path", "model_size"}:
            value = _string_default_from_expression(
                keyword.value,
                bindings,
                parameters,
            )
            if value:
                return value
    return ""


def _faster_whisper_plan_for_literal(model: str) -> dict[str, Any]:
    normalized = model.strip().lower()
    variant_aliases = {
        "tiny": "tiny",
        "systran/faster-whisper-tiny": "tiny",
        "small": "fast",
        "systran/faster-whisper-small": "fast",
        "medium": "balanced",
        "systran/faster-whisper-medium": "balanced",
        "large-v3": "accuracy",
        "large-v3-turbo": "accuracy",
        "systran/faster-whisper-large-v3": "accuracy",
    }
    variant_name = variant_aliases.get(normalized)
    if not variant_name:
        return {}
    variant = (
        {
            "model_id": "Systran/faster-whisper-tiny",
            "model_revision": "d90ca5fe260221311c53c58e660288d3deb8d356",
        }
        if variant_name == "tiny"
        else FASTER_WHISPER_PLAN["model_variants"][variant_name]
    )
    plan = _copy_value(FASTER_WHISPER_PLAN)
    plan["model_id"] = variant["model_id"]
    plan["model_revision"] = variant["model_revision"]
    # The uploaded source selected this literal model itself, so prefetch that
    # exact revision rather than applying the generated-task profile policy.
    plan["model_variants"] = {}
    plan["runtime_selection"] = {}
    plan["resolution"] = {
        "source": "inlumen-uploaded-python-inference",
        "status": "resolved",
        "detected_library": "faster_whisper",
        "detected_constructor": "WhisperModel",
        "detected_model_literal": model,
    }
    return plan


def _infer_pinned_transformers_plan(
    tree: ast.AST,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Resolve reviewed Transformers models from direct code or CLI defaults."""
    direct_names: set[str] = set()
    pipeline_names: set[str] = set()
    module_names: set[str] = set()
    bindings = _string_default_bindings(tree, parameters)
    cli_defaults = _cli_argument_defaults(tree, parameters)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "transformers":
            for imported in node.names:
                if imported.name in {"AutoModel", "AutoTokenizer"}:
                    direct_names.add(imported.asname or imported.name)
                elif imported.name == "pipeline":
                    pipeline_names.add(imported.asname or imported.name)
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "transformers":
                    module_names.add(imported.asname or imported.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        model_id = ""
        revision = ""
        if _is_transformers_from_pretrained(node.func, direct_names, module_names):
            model_id = _call_string_argument(
                node,
                {"pretrained_model_name_or_path"},
                parameters,
                bindings=bindings,
                cli_defaults=cli_defaults,
            )
            revision = _call_string_keyword(
                node,
                "revision",
                parameters,
                bindings=bindings,
                cli_defaults=cli_defaults,
            )
        elif _is_transformers_pipeline(node.func, pipeline_names, module_names):
            model_id = _call_string_keyword(
                node,
                "model",
                parameters,
                bindings=bindings,
                cli_defaults=cli_defaults,
            )
            revision = _call_string_keyword(
                node,
                "revision",
                parameters,
                bindings=bindings,
                cli_defaults=cli_defaults,
            )
        if model_id and revision:
            return _pinned_transformers_plan(model_id, revision)
        if model_id:
            reviewed_plan = _reviewed_transformers_plan_for_model(model_id)
            if reviewed_plan:
                return reviewed_plan
    return {}


def _is_transformers_from_pretrained(
    function: ast.expr,
    direct_names: set[str],
    module_names: set[str],
) -> bool:
    if not isinstance(function, ast.Attribute) or function.attr != "from_pretrained":
        return False
    if isinstance(function.value, ast.Name):
        return function.value.id in direct_names
    return (
        isinstance(function.value, ast.Attribute)
        and isinstance(function.value.value, ast.Name)
        and function.value.value.id in module_names
        and function.value.attr in {"AutoModel", "AutoTokenizer"}
    )


def _is_transformers_pipeline(
    function: ast.expr,
    direct_names: set[str],
    module_names: set[str],
) -> bool:
    return (
        isinstance(function, ast.Name) and function.id in direct_names
    ) or (
        isinstance(function, ast.Attribute)
        and function.attr == "pipeline"
        and isinstance(function.value, ast.Name)
        and function.value.id in module_names
    )


def _call_string_argument(
    call: ast.Call,
    keyword_names: set[str],
    parameters: dict[str, Any],
    *,
    bindings: dict[str, str] | None = None,
    cli_defaults: dict[str, str] | None = None,
) -> str:
    if call.args:
        value = _runtime_string_value(
            call.args[0],
            parameters,
            bindings=bindings,
            cli_defaults=cli_defaults,
        )
        if value:
            return value
    for keyword in call.keywords:
        if keyword.arg in keyword_names:
            value = _runtime_string_value(
                keyword.value,
                parameters,
                bindings=bindings,
                cli_defaults=cli_defaults,
            )
            if value:
                return value
    return ""


def _call_string_keyword(
    call: ast.Call,
    name: str,
    parameters: dict[str, Any],
    *,
    bindings: dict[str, str] | None = None,
    cli_defaults: dict[str, str] | None = None,
) -> str:
    for keyword in call.keywords:
        if keyword.arg == name:
            return _runtime_string_value(
                keyword.value,
                parameters,
                bindings=bindings,
                cli_defaults=cli_defaults,
            )
    return ""


def _runtime_string_value(
    expression: ast.expr,
    parameters: dict[str, Any],
    *,
    bindings: dict[str, str] | None = None,
    cli_defaults: dict[str, str] | None = None,
) -> str:
    """Resolve a literal, binding, or conventional ``args.option`` value."""
    if (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Name)
        and expression.value.id == "args"
    ):
        return (cli_defaults or {}).get(expression.attr, "")
    return _string_default_from_expression(
        expression,
        bindings or {},
        parameters,
    )


def _pinned_transformers_plan(model_id: str, revision: str) -> dict[str, Any]:
    return {
        "schema_version": IMPLEMENTATION_PLAN_SCHEMA,
        "execution_profile": "trusted_heavy_model",
        "resource_class": "heavy_cpu_or_gpu",
        "task": "model-inference",
        "domain": "custom",
        "adapter_id": "huggingface-transformers",
        "adapter_version": "1",
        "framework": "transformers",
        "model_id": model_id,
        "model_revision": revision,
        "artifact_policy": _copy_value(LOCAL_MODEL_ARTIFACT_POLICY),
        "resolution": {
            "source": "inlumen-uploaded-python-inference",
            "status": "resolved",
            "detected_library": "transformers",
            "requirement": "explicit model id and revision",
        },
    }


def _reviewed_transformers_plan_for_model(model_id: str) -> dict[str, Any]:
    """Pin known local Transformers model aliases used by uploaded code.

    A user script may use the ordinary Hugging Face example form
    ``pipeline(..., model=args.model)`` without providing an explicit revision.
    We accept only reviewed aliases here and turn them into reproducible local
    artifacts during bundle preflight; arbitrary aliases remain unguessed.
    """
    reviewed_models = {
        "distilbert/distilbert-base-uncased-finetuned-sst-2-english": {
            "revision": "714eb0fa89d2f80546fda750413ed43d93601a13",
            "task": "text-classification",
            "domain": "sentiment-analysis",
            "adapter_id": "transformers-sst2-sentiment",
        },
        "openai/whisper-small": {
            "revision": "973afd24965f72e36ca33b3055d56a652f456b4d",
            "task": "automatic-speech-recognition",
            "domain": "speech-to-text",
            "adapter_id": "huggingface-transformers",
            "required_system_packages": ["ffmpeg"],
        },
    }
    reviewed = reviewed_models.get(model_id.strip().lower())
    if reviewed is None:
        return {}
    plan = _pinned_transformers_plan(model_id, reviewed["revision"])
    plan.update({
        "task": reviewed["task"],
        "domain": reviewed["domain"],
        "adapter_id": reviewed["adapter_id"],
        "required_packages": [
            "transformers>=4.48,<5",
            "torch>=2.5,<3",
            "huggingface-hub>=0.36,<1",
        ],
        "required_system_packages": list(
            reviewed.get("required_system_packages") or []
        ),
    })
    plan["resolution"] = {
        "source": "inlumen-uploaded-python-inference",
        "status": "resolved",
        "detected_library": "transformers",
        "detected_constructor": "pipeline",
        "detected_model_literal": model_id,
    }
    return plan


# Build-time source inference is a registry rather than a chain of special
# cases.  Each detector owns the library-specific syntax and resolves only to
# a reviewed, pinned local artifact.  New local runtimes can be added without
# changing deployment orchestration or the uploaded-task contract.
_MODEL_SOURCE_PLAN_DETECTORS = (
    _infer_faster_whisper_plan,
    _infer_pinned_transformers_plan,
)

ROBERTA_SENTIMENT_PLAN: dict[str, Any] = {
    "schema_version": IMPLEMENTATION_PLAN_SCHEMA,
    "execution_profile": "trusted_heavy_model",
    "resource_class": "heavy_cpu_or_gpu",
    "task": "text-classification",
    "domain": "sentiment-analysis",
    "adapter_id": "transformers-roberta-sentiment",
    "adapter_version": "1",
    "framework": "transformers",
    "model_id": "cardiffnlp/twitter-roberta-base-sentiment-latest",
    "model_revision": "3216a57f2a0d9c45a2e6c20157c20c49fb4bf9c7",
    "device": "auto",
    "precision": "float32",
    "artifact_policy": LOCAL_MODEL_ARTIFACT_POLICY,
    "required_packages": [
        "transformers==5.14.1",
        "torch==2.13.0",
        "huggingface-hub==1.25.1",
    ],
    "inference_parameters": {
        "max_length": 512,
        "overlap_tokens": 64,
    },
    "quality_policy": {
        "schema_version": QUALITY_POLICY_SCHEMA,
        "min_top_class_probability": 0.45,
        "low_confidence_action": "warn",
    },
    "resolution": {
        "source": "inlumen-trusted-adapter-registry",
        "status": "resolved",
    },
}

CLASSICAL_ML_PLAN: dict[str, Any] = {
    "schema_version": IMPLEMENTATION_PLAN_SCHEMA,
    "execution_profile": "classical_ml",
    "resource_class": "lightweight_cpu",
    "task": "supervised-learning",
    "domain": "tabular",
    "framework": "scikit-learn",
    "estimator_family": "auto",
    "selection_strategy": "cross_validation",
    "required_packages": [
        "scikit-learn>=1.4,<2",
        "pandas>=2,<3",
        "numpy>=1.26,<3",
        "joblib>=1.3,<2",
    ],
    "inference_parameters": {
        "random_state": 42,
        "test_size": 0.2,
    },
    "quality_policy": {
        "schema_version": QUALITY_POLICY_SCHEMA,
        "require_non_empty_training_data": True,
        "require_holdout_evaluation": True,
        "require_serialized_estimator": True,
    },
    "resolution": {
        "source": "inlumen-routing-policy",
        "status": "resolved",
    },
}


def resolve_implementation_plan(
    plan: Any,
    *,
    label: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Resolve recognized model tasks without rejecting unknown modalities."""
    candidate = dict(plan) if isinstance(plan, dict) else {}
    node_text = " ".join((str(label or ""), str(description or ""))).lower()
    responsibility_text = str(label or description or "").strip().lower()
    task_text = " ".join(
        str(value or "")
        for value in (
            label,
            candidate.get("task"),
            candidate.get("domain"),
            candidate.get("adapter_id"),
            candidate.get("framework"),
            candidate.get("model_id"),
        )
    ).lower()
    if _is_classical_training_task(node_text):
        if _explicit_heavy_model_request(node_text):
            return _proposed_custom_plan(candidate)
        return _resolve_classical_ml_plan(candidate)

    if _is_report_task(responsibility_text):
        return _without_mismatched_trusted_adapter(candidate)
    if _is_sentiment_task(responsibility_text):
        return _merge_runtime_preferences(ROBERTA_SENTIMENT_PLAN, candidate)
    if _is_asr_task(responsibility_text):
        return _merge_runtime_preferences(FASTER_WHISPER_PLAN, candidate)

    if _is_sentiment_task(task_text):
        return _merge_runtime_preferences(ROBERTA_SENTIMENT_PLAN, candidate)
    if _is_asr_task(task_text):
        return _merge_runtime_preferences(FASTER_WHISPER_PLAN, candidate)
    return _proposed_custom_plan(candidate)


def _is_classical_training_task(text: str) -> bool:
    explicit_phrase = any(
        token in text
        for token in (
            "model training",
            "train model",
            "trains a ",
            "training a ",
            "classifier training",
            "regressor training",
            "fit a model",
            "fit the model",
        )
    )
    training_intent = any(token in text for token in ("train", "training", "fit"))
    learned_task = any(
        token in text
        for token in ("model", "classifier", "regressor", "prediction", "predictor")
    )
    return explicit_phrase or (training_intent and learned_task)


def _explicit_heavy_model_request(text: str) -> bool:
    return any(
        token in text
        for token in (
            "transformer",
            "neural network",
            "deep learning",
            "pytorch",
            "tensorflow",
            "fine-tun",
            "pretrained model",
            "pre-trained model",
            "bert",
        )
    )


def _resolve_classical_ml_plan(candidate: dict[str, Any]) -> dict[str, Any]:
    resolved = {key: _copy_value(value) for key, value in CLASSICAL_ML_PLAN.items()}
    if str(candidate.get("kind") or "").strip():
        resolved["kind"] = str(candidate["kind"]).strip()
    for key in ("task", "domain"):
        value = str(candidate.get(key) or "").strip()
        if value:
            resolved[key] = value
    estimator_family = str(candidate.get("estimator_family") or "").strip().lower()
    if estimator_family in {
        "auto",
        "linear",
        "logistic_regression",
        "random_forest",
        "gradient_boosting",
    }:
        resolved["estimator_family"] = estimator_family
    configured = candidate.get("inference_parameters")
    if isinstance(configured, dict):
        supported = set(resolved["inference_parameters"])
        for key, value in configured.items():
            if key in supported:
                resolved["inference_parameters"][key] = _copy_value(value)
    proposed_model_id = str(candidate.get("model_id") or "").strip()
    if proposed_model_id:
        resolved["resolution"]["replaced_proposed_model_id"] = proposed_model_id
        resolved["resolution"]["reason"] = (
            "Training intent and structured-data contract default to classical ML."
        )
    return resolved


def _proposed_custom_plan(candidate: dict[str, Any]) -> dict[str, Any]:
    # Preserve unsupported/custom implementation payloads byte-for-byte at the
    # semantic level. They remain advisory because only registry-resolved plans
    # receive a trusted execution profile.
    return {key: _copy_value(value) for key, value in candidate.items()}


def _is_asr_task(text: str) -> bool:
    return any(
        token in text
        for token in (
            "speech-to-text",
            "speech to text",
            "automatic-speech-recognition",
            "automatic speech recognition",
            "whisper",
            "audio transcription",
            "speech transcription",
            "transcribe audio",
            "transcribe speech",
            "transcribe recording",
            "transcription model",
            "transcription inference",
        )
    )


def _is_sentiment_task(text: str) -> bool:
    return "sentiment" in text or "twitter-roberta-base-sentiment" in text


def _is_report_task(text: str) -> bool:
    return any(token in text for token in ("report", "result compilation"))


def _without_mismatched_trusted_adapter(candidate: dict[str, Any]) -> dict[str, Any]:
    if (
        candidate.get("schema_version") == IMPLEMENTATION_PLAN_SCHEMA
        and str(candidate.get("adapter_id") or "")
        in {"faster-whisper", "transformers-roberta-sentiment"}
    ):
        return {}
    return _proposed_custom_plan(candidate)


def _merge_runtime_preferences(
    trusted: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    resolved = {key: _copy_value(value) for key, value in trusted.items()}
    if str(candidate.get("kind") or "").strip():
        resolved["kind"] = str(candidate["kind"]).strip()
    if str(candidate.get("device") or "").lower() in {"auto", "cpu", "cuda"}:
        resolved["device"] = str(candidate["device"]).lower()

    configured = candidate.get("inference_parameters")
    if isinstance(configured, dict):
        supported = set(resolved["inference_parameters"])
        for key, value in configured.items():
            if key in supported:
                resolved["inference_parameters"][key] = _copy_value(value)
    return resolved


def _copy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    return value
