from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

from .model_plans import (
    is_asr_task_text,
    model_plan_requires_exact_model,
    resolve_implementation_plan,
    trusted_adapter_id,
)
from .schemas import (
    ExpectedArtifact,
    FileDescriptor,
    NodeDescriptor,
    is_input_node_kind,
)

AUDIO_FORMATS = {"wav", "wave", "mp3", "m4a", "aac", "flac", "ogg"}
DOCUMENT_FORMATS = {"pdf", "doc", "docx", "txt", "md", "rtf"}
IMAGE_FORMATS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}


@dataclass(frozen=True)
class TaskProfile:
    name: str
    packages: tuple[str, ...] = ()
    guidance: tuple[str, ...] = ()


TASK_PROFILES = {
    "generic": TaskProfile(
        name="generic",
        guidance=(
            "Implement the described transformation against the real manifest inputs.",
        ),
    ),
    "subpipeline": TaskProfile(
        name="subpipeline",
        guidance=(
            "Implement the pinned reusable pipeline graph in node.subpipeline.resolved_graph; do not replace it with a pass-through or placeholder.",
            "Respect the declared public interface bindings: parent inputs enter through nested source boundaries and nested destination values become parent outputs.",
            "Execute nested components in dependency order and preserve their explicit port types and implementation plans.",
            "Fail clearly when the pinned reference cannot be resolved or its public interface is inconsistent.",
        ),
    ),
    "ingestion": TaskProfile(
        name="ingestion",
        guidance=(
            "Copy binary source artifacts byte-for-byte.",
            "Validate that the selected input exists and is non-empty.",
        ),
    ),
    "audio_preprocessing": TaskProfile(
        name="audio_preprocessing",
        packages=("scipy>=1.11.0", "soundfile>=0.12.1"),
        guidance=(
            "Parse and process the complete audio stream, not just its header.",
            "Produce a structurally valid, non-empty audio file.",
            "Apply every requested resampling, channel, filtering, gain, and trimming step.",
            "Use scipy.signal.resample_poly for sample-rate conversion; derive integer up/down factors with gcd.",
            "Design Butterworth filters with the fs keyword and output='sos', then apply sosfilt or sosfiltfilt.",
            "Require 0 < low_cutoff < high_cutoff < sample_rate / 2; when an upper cutoff reaches Nyquist, use a high-pass filter instead of an invalid band-pass.",
            "Do not denoise, filter, compress, normalize, or trim unless the pipeline description or parameters request that operation.",
            "Validate non-empty finite output, expected channel count, target sample rate, and duration tolerance before writing it.",
        ),
    ),
    "speech_to_text": TaskProfile(
        name="speech_to_text",
        guidance=(
            "Honor the reviewed implementation plan exactly.",
            "Load the selected model and transcribe the actual audio input.",
            "Return real recognizer output with requested segment or word timestamps.",
            "Record the exact model, revision, device, precision, and inference parameters.",
            "Model or import failures must raise an error; never fabricate a transcript.",
        ),
    ),
    "sentiment": TaskProfile(
        name="sentiment",
        guidance=(
            "Honor the reviewed implementation plan exactly.",
            "Analyze the complete transcript using overlapping chunks when necessary.",
            "Preserve per-class and per-segment scores and document aggregation.",
            "Never substitute a hardcoded lexicon or fixed label.",
        ),
    ),
    "pdf_extraction": TaskProfile(
        name="pdf_extraction",
        packages=("pypdf>=5,<7",),
        guidance=(
            "Extract every PDF page with a real PDF parser.",
            "Return page-level text and fail clearly when no text is extractable.",
        ),
    ),
    "document_chunking": TaskProfile(
        name="document_chunking",
        guidance=(
            "Chunk the actual document text with deterministic overlap and stable IDs.",
        ),
    ),
    "document_indexing": TaskProfile(
        name="document_indexing",
        packages=("scikit-learn",),
        guidance=(
            "Fit a real index on the input chunks and serialize the fitted artifacts.",
        ),
    ),
    "question_answering": TaskProfile(
        name="question_answering",
        guidance=(
            "Answer only from the supplied searchable index and preserve source and page citations.",
            "Fail clearly when the retrieved context does not support an answer.",
        ),
    ),
    "model_training": TaskProfile(
        name="model_training",
        packages=("scikit-learn>=1.4,<2",),
        guidance=(
            "Fit the estimator on real input rows and calculate metrics from predictions.",
        ),
    ),
    "image_processing": TaskProfile(
        name="image_processing",
        packages=("pillow",),
        guidance=(
            "Open and validate the actual image before applying the transformation.",
        ),
    ),
    "report": TaskProfile(
        name="report",
        guidance=(
            "Assemble the report only from upstream artifacts and measured metadata.",
            "Preserve transcription, sentiment, confidence, audio, and timing details.",
        ),
    ),
    "summary": TaskProfile(
        name="summary",
        guidance=("Build the summary from the actual upstream content.",),
    ),
}


def implementation_plan_for_node(
    node: NodeDescriptor,
    inputs: list[FileDescriptor] | None = None,
) -> dict[str, Any]:
    plan: dict[str, Any] = {}
    if node.implementation:
        plan = dict(node.implementation)
    else:
        for key in ("model_plan", "implementation"):
            nested = node.parameters.get(key)
            if isinstance(nested, dict):
                plan = dict(nested)
                break
    return resolve_implementation_plan(
        plan,
        label=(
            "Model Training"
            if classify_node_task(node, inputs or []) == "model_training"
            else node.label
        ),
        description=node.description,
    )


def classify_node_task(
    node: NodeDescriptor,
    inputs: list[FileDescriptor],
) -> str:
    if node.type == "subpipeline":
        return "subpipeline"
    # The label states the node's responsibility. Descriptions frequently
    # mention upstream/downstream tasks and must not override that responsibility.
    text = str(node.label or node.description or "").strip().lower()
    formats = {str(item.format or "").lower() for item in inputs}
    kinds = {str(item.kind or "").lower() for item in inputs}
    raw_plan = node.implementation if isinstance(node.implementation, dict) else {}
    if not raw_plan:
        for key in ("model_plan", "implementation"):
            candidate = node.parameters.get(key)
            if isinstance(candidate, dict):
                raw_plan = candidate
                break
    if is_input_node_kind(node.type):
        return "ingestion"
    if any(token in text for token in ("report", "result compilation")):
        return "report"
    if raw_plan.get("execution_profile") == "classical_ml":
        return "model_training"
    if any(
        token in text
        for token in (
            "train model",
            "model training",
            "classifier training",
            "regressor training",
            "training a model",
            "fit a model",
        )
    ):
        return "model_training"
    if "sentiment" in text:
        return "sentiment"
    if formats & AUDIO_FORMATS and any(
        token in text
        for token in ("preprocess", "clean", "normalize", "resample", "denoise")
    ):
        return "audio_preprocessing"
    if is_asr_task_text(text):
        return "speech_to_text"
    if (
        ("table" in kinds or formats & {"csv", "tsv", "parquet"})
        and any(
            token in text
            for token in (
                "prediction",
                "predictive model",
                "risk scoring",
                "classification",
                "regression",
            )
        )
    ):
        return "model_training"
    if any(token in text for token in ("chunk", "split document", "segment document")):
        return "document_chunking"
    if any(
        token in text for token in ("index", "embedding", "vector store", "vectorize")
    ):
        return "document_indexing"
    if any(
        token in text
        for token in ("question answering", "answer question", "retrieval augmented")
    ):
        return "question_answering"
    if "pdf" in formats and any(
        token in text for token in ("extract", "parse", "read")
    ):
        return "pdf_extraction"
    if formats & IMAGE_FORMATS or any(
        token in text for token in ("image", "photo", "picture")
    ):
        return "image_processing"
    if any(token in text for token in ("summary", "summarize", "summarise")):
        return "summary"
    return "generic"


def task_profile_payload(
    node: NodeDescriptor,
    inputs: list[FileDescriptor],
) -> dict[str, Any]:
    profile = TASK_PROFILES[classify_node_task(node, inputs)]
    packages = list(profile.packages)
    guidance = list(profile.guidance)
    if profile.name == "document_chunking" and any(
        str(item.format or "").lower() == "pdf" for item in inputs
    ):
        packages.append("pypdf>=5,<7")
        guidance.extend(
            [
                "Parse every PDF page with pypdf.PdfReader before chunking.",
                "Never decode, regex, or scan raw PDF bytes as document text.",
            ]
        )
    return {
        "name": profile.name,
        "required_packages": packages,
        "implementation_rules": guidance,
        "selected_implementation_plan": implementation_plan_for_node(node, inputs),
    }


def requirement_name(requirement: str) -> str:
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)", requirement)
    return match.group(1).lower().replace("_", "-") if match else ""


def required_packages_for_node(
    node: NodeDescriptor,
    inputs: list[FileDescriptor],
) -> list[str]:
    profile = TASK_PROFILES[classify_node_task(node, inputs)]
    plan = implementation_plan_for_node(node, inputs)
    planned = plan.get("required_packages")
    candidates = [
        *profile.packages,
        *(
            ["pypdf>=5,<7"]
            if profile.name == "document_chunking"
            and any(str(item.format or "").lower() == "pdf" for item in inputs)
            else []
        ),
        *([str(item) for item in planned] if isinstance(planned, list) else []),
    ]
    packages: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        name = requirement_name(candidate)
        if name and name not in seen:
            packages.append(str(candidate).strip())
            seen.add(name)
    return packages


def source_semantic_errors(
    node: NodeDescriptor,
    inputs: list[FileDescriptor],
    source: str,
    expected_outputs: list[ExpectedArtifact] | None = None,
    function_name: str | None = None,
) -> list[str]:
    task = classify_node_task(node, inputs)
    errors: list[str] = []
    try:
        tree = ast.parse(source, filename=f"{node.flow_id}/main.py")
    except SyntaxError:
        return errors
    string_literals = {
        item.value.lower()
        for item in ast.walk(tree)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    calls = [item for item in ast.walk(tree) if isinstance(item, ast.Call)]
    call_names = {_call_name(item.func) for item in calls}
    errors.extend(_runtime_model_acquisition_errors(node.flow_id, tree, calls))
    plan = implementation_plan_for_node(node, inputs)
    model_id = str(plan.get("model_id") or "").strip()
    model_revision = str(plan.get("model_revision") or "").strip()
    if (
        model_plan_requires_exact_model(plan)
        and model_id.lower() not in string_literals
    ):
        errors.append(
            f"Node {node.flow_id} does not load reviewed model_id {model_id!r}."
        )
    placeholders = (
        "dummy transcription",
        "test transcription",
        "placeholder prediction",
        "placeholder result",
        "mock result",
        "synthetic success",
        "model not available in runtime",
    )
    marker = next(
        (
            placeholder
            for placeholder in placeholders
            if any(placeholder in literal for literal in string_literals)
        ),
        None,
    )
    if marker:
        errors.append(
            f"Node {node.flow_id} contains placeholder or synthetic-success logic: {marker}."
        )
    if task == "speech_to_text":
        if any(
            _exception_handler_names(item)
            & {"ImportError", "ModuleNotFoundError"}
            for item in ast.walk(tree)
            if isinstance(item, ast.ExceptHandler)
        ):
            errors.append(
                f"Node {node.flow_id} hides a required speech-recognition dependency failure."
            )
        if not call_names & {"pipeline", "generate", "transcribe"}:
            errors.append(
                f"Node {node.flow_id} does not invoke a speech-recognition model."
            )
        if trusted_adapter_id(plan) == "faster-whisper":
            errors.extend(
                _trusted_model_adapter_errors(
                    node.flow_id,
                    tree,
                    call_names,
                    model_revision=model_revision,
                    required_imports={"faster_whisper"},
                    required_calls={"WhisperModel", "resolve_local_model", "transcribe"},
                    required_symbols={"WhisperModel"},
                    forbidden_calls={"pipeline"},
                    forbidden_imports={"huggingface_hub"},
                )
            )
    if task == "sentiment":
        identifiers = {
            item.id.lower()
            for item in ast.walk(tree)
            if isinstance(item, ast.Name)
        }
        if identifiers & {
            "positive_words",
            "negative_words",
            "positive_lexicon",
            "negative_lexicon",
        }:
            errors.append(f"Node {node.flow_id} uses a hardcoded sentiment lexicon.")
        if trusted_adapter_id(plan) == "transformers-roberta-sentiment":
            errors.extend(
                _trusted_model_adapter_errors(
                    node.flow_id,
                    tree,
                    call_names,
                    model_revision=model_revision,
                    required_imports={"transformers", "torch"},
                    required_calls={"from_pretrained", "resolve_local_model", "softmax"},
                    required_symbols={
                        "AutoTokenizer",
                        "AutoModelForSequenceClassification",
                    },
                    forbidden_calls=set(),
                    forbidden_imports={"huggingface_hub"},
                )
            )
    if task == "audio_preprocessing":
        errors.extend(_audio_preprocessing_semantic_errors(node.flow_id, calls))
    if task == "document_chunking" and any(
        str(item.format or "").lower() == "pdf" for item in inputs
    ):
        imported_roots = _imported_roots(tree)
        if "pypdf" not in imported_roots or "PdfReader" not in call_names:
            errors.append(
                f"Node {node.flow_id} must parse PDF inputs with pypdf.PdfReader before chunking."
            )
        raw_pdf_calls = sorted(call_names & {"read_bytes", "decode"})
        if raw_pdf_calls:
            errors.append(
                f"Node {node.flow_id} treats raw PDF bytes as text: "
                + ", ".join(raw_pdf_calls)
                + "."
            )
    if task == "model_training" and plan.get("execution_profile") == "classical_ml":
        if "fit" not in call_names:
            errors.append(
                f"Node {node.flow_id} does not fit a classical ML estimator."
            )
        imported_roots = _imported_roots(tree)
        forbidden = sorted(imported_roots & {"transformers", "torch", "tensorflow"})
        if forbidden:
            errors.append(
                f"Node {node.flow_id} classical ML route imports heavy frameworks: "
                + ", ".join(forbidden)
            )
    errors.extend(
        _statically_verifiable_json_contract_errors(
            node.flow_id,
            tree,
            expected_outputs or [],
            function_name=function_name,
        )
    )
    return errors


def _statically_verifiable_json_contract_errors(
    flow_id: str,
    tree: ast.AST,
    expected_outputs: list[ExpectedArtifact],
    *,
    function_name: str | None,
) -> list[str]:
    """Compare literal JSON payloads with their declared required fields.

    Runtime validation remains authoritative. This check covers model-backed
    nodes whose execution is intentionally deferred during generation: when a
    function writes a named dict through json.dump/json.dumps, its literal keys
    are knowable without importing or downloading the model.
    """
    expected_json = [
        output
        for output in expected_outputs
        if output.kind == "json"
        and isinstance(output.schema, dict)
        and output.schema.get("type") == "object"
        and output.schema.get("required")
    ]
    if not expected_json:
        return []

    functions = [
        item
        for item in getattr(tree, "body", [])
        if isinstance(item, ast.FunctionDef)
        and (not function_name or item.name == function_name)
    ]
    candidate_key_sets: list[set[str]] = []
    for function in functions:
        assignments: dict[str, ast.Dict] = {}
        for item in ast.walk(function):
            if (
                isinstance(item, ast.Assign)
                and isinstance(item.value, ast.Dict)
                and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)
            ):
                assignments[item.targets[0].id] = item.value
            elif (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and isinstance(item.value, ast.Dict)
            ):
                assignments[item.target.id] = item.value

        for call in (
            item for item in ast.walk(function) if isinstance(item, ast.Call)
        ):
            if _call_name(call.func) not in {"dump", "dumps"} or not call.args:
                continue
            payload = call.args[0]
            if isinstance(payload, ast.Name):
                payload = assignments.get(payload.id)
            if isinstance(payload, ast.Dict):
                candidate_key_sets.append(_literal_dict_keys(payload))

    if not candidate_key_sets:
        return []

    errors: list[str] = []
    for output in expected_json:
        required = {
            str(key)
            for key in output.schema.get("required", [])
            if isinstance(key, str)
        }
        if any(required <= candidate for candidate in candidate_key_sets):
            continue
        best_candidate = max(
            candidate_key_sets,
            key=lambda candidate: len(required & candidate),
        )
        missing = sorted(required - best_candidate)
        errors.append(
            f"Node {flow_id} writes JSON output {output.name!r} without declared "
            f"required fields: {', '.join(missing)}."
        )
    return errors


def _literal_dict_keys(node: ast.Dict) -> set[str]:
    return {
        str(key.value)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _call_name(function: ast.expr) -> str:
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in item.names)
        elif isinstance(item, ast.ImportFrom) and item.module:
            roots.add(item.module.split(".", 1)[0])
    return roots


def _trusted_model_adapter_errors(
    flow_id: str,
    tree: ast.AST,
    call_names: set[str],
    *,
    model_revision: str,
    required_imports: set[str],
    required_calls: set[str],
    required_symbols: set[str],
    forbidden_calls: set[str],
    forbidden_imports: set[str],
) -> list[str]:
    errors: list[str] = []
    imported_roots = _imported_roots(tree)
    missing_imports = sorted(required_imports - imported_roots)
    if missing_imports:
        errors.append(
            f"Node {flow_id} is missing trusted adapter imports: "
            + ", ".join(missing_imports)
        )
    used_forbidden_imports = sorted(forbidden_imports & imported_roots)
    if used_forbidden_imports:
        errors.append(
            f"Node {flow_id} imports runtime model-download clients: "
            + ", ".join(used_forbidden_imports)
        )
    missing_calls = sorted(required_calls - call_names)
    if missing_calls:
        errors.append(
            f"Node {flow_id} is missing trusted adapter calls: "
            + ", ".join(missing_calls)
        )
    symbols = {
        item.id
        for item in ast.walk(tree)
        if isinstance(item, ast.Name)
    }
    missing_symbols = sorted(required_symbols - symbols)
    if missing_symbols:
        errors.append(
            f"Node {flow_id} is missing trusted adapter symbols: "
            + ", ".join(missing_symbols)
        )
    used_forbidden = sorted(forbidden_calls & call_names)
    if used_forbidden:
        errors.append(
            f"Node {flow_id} mixes incompatible model adapters: "
            + ", ".join(used_forbidden)
        )
    revision_literals = {
        item.value
        for item in ast.walk(tree)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    if model_revision and model_revision not in revision_literals:
        errors.append(
            f"Node {flow_id} does not pin reviewed model_revision "
            f"{model_revision!r}."
        )
    return errors


def _runtime_model_acquisition_errors(
    flow_id: str,
    tree: ast.AST,
    calls: list[ast.Call],
) -> list[str]:
    """Reject model-hub acquisition from generated node runtimes.

    Model snapshots belong to deployment preflight. Node execution must consume
    a local path so an inference run never depends on provider availability.
    """
    errors: list[str] = []
    imported_roots = _imported_roots(tree)
    if "huggingface_hub" in imported_roots:
        errors.append(
            f"Node {flow_id} imports huggingface_hub at runtime; acquire model "
            "snapshots during deployment preflight instead."
        )

    download_calls = {
        "cached_download",
        "download_url_to_file",
        "hf_hub_download",
        "load_state_dict_from_url",
        "snapshot_download",
    }
    used_download_calls = sorted(
        {_call_name(call.func) for call in calls} & download_calls
    )
    if used_download_calls:
        errors.append(
            f"Node {flow_id} downloads model artifacts during runtime: "
            + ", ".join(used_download_calls)
            + "."
        )

    local_only_frameworks = {
        "diffusers",
        "sentence_transformers",
        "transformers",
    }
    if imported_roots & local_only_frameworks:
        for call in calls:
            if _call_name(call.func) != "from_pretrained":
                continue
            local_only = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "local_files_only"
                ),
                None,
            )
            if not (
                isinstance(local_only, ast.Constant)
                and local_only.value is True
            ):
                errors.append(
                    f"Node {flow_id} calls from_pretrained without "
                    "local_files_only=True."
                )
                break

    if "transformers" in imported_roots:
        for call in calls:
            if _call_name(call.func) != "pipeline":
                continue
            model_value = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "model"),
                call.args[1] if len(call.args) > 1 else None,
            )
            if isinstance(model_value, ast.Constant) and isinstance(
                model_value.value, str
            ):
                errors.append(
                    f"Node {flow_id} passes a model identifier directly to "
                    "transformers.pipeline; load a verified local model first."
                )
                break

    if "faster_whisper" in imported_roots:
        for call in calls:
            if _call_name(call.func) != "WhisperModel" or not call.args:
                continue
            if isinstance(call.args[0], ast.Constant) and isinstance(
                call.args[0].value, str
            ):
                errors.append(
                    f"Node {flow_id} passes a model identifier directly to "
                    "WhisperModel; load a verified local snapshot path first."
                )
                break
    return errors


def _exception_handler_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    if isinstance(handler.type, ast.Tuple):
        candidates = handler.type.elts
    else:
        candidates = [handler.type]
    return {
        item.id
        for item in candidates
        if isinstance(item, ast.Name)
    }


def _keyword_literal(call: ast.Call, name: str) -> Any:
    keyword = next((item for item in call.keywords if item.arg == name), None)
    if keyword is None:
        return None
    try:
        return ast.literal_eval(keyword.value)
    except (TypeError, ValueError):
        return None


def _audio_preprocessing_semantic_errors(
    flow_id: str,
    calls: list[ast.Call],
) -> list[str]:
    errors: list[str] = []
    call_names = {_call_name(call.func) for call in calls}
    unsupported_resamplers = call_names & {"resample"}
    if unsupported_resamplers and "resample_poly" not in call_names:
        errors.append(
            f"Node {flow_id} must use scipy.signal.resample_poly for audio sample-rate conversion."
        )

    butter_calls = [
        call for call in calls if _call_name(call.func) == "butter"
    ]
    for call in butter_calls:
        if not any(keyword.arg == "fs" for keyword in call.keywords):
            errors.append(
                f"Node {flow_id} must design Butterworth filters with an explicit fs sample rate."
            )
        if _keyword_literal(call, "output") != "sos":
            errors.append(
                f"Node {flow_id} must request output='sos' from scipy.signal.butter."
            )
    if butter_calls and not call_names & {"sosfilt", "sosfiltfilt"}:
        errors.append(
            f"Node {flow_id} must apply Butterworth filters using sosfilt or sosfiltfilt."
        )

    ambiguous_fft_calls = [
        call
        for call in calls
        if _call_name(call.func) in {"rfft", "irfft"}
        and not any(keyword.arg == "axis" for keyword in call.keywords)
    ]
    if ambiguous_fft_calls:
        errors.append(
            f"Node {flow_id} must give rfft/irfft an explicit sample axis; "
            "the default last axis can expand an (samples, 1) waveform into a "
            "multi-terabyte array."
        )
    return errors
