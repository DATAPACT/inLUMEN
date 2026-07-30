from __future__ import annotations

from typing import Any


IMPLEMENTATION_PLAN_SCHEMA = "inlumen.implementation-plan@1"
QUALITY_POLICY_SCHEMA = "inlumen.quality-policy@1"

FASTER_WHISPER_PLAN: dict[str, Any] = {
    "schema_version": IMPLEMENTATION_PLAN_SCHEMA,
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

ROBERTA_SENTIMENT_PLAN: dict[str, Any] = {
    "schema_version": IMPLEMENTATION_PLAN_SCHEMA,
    "task": "text-classification",
    "domain": "sentiment-analysis",
    "adapter_id": "transformers-roberta-sentiment",
    "adapter_version": "1",
    "framework": "transformers",
    "model_id": "cardiffnlp/twitter-roberta-base-sentiment-latest",
    "model_revision": "3216a57f2a0d9c45a2e6c20157c20c49fb4bf9c7",
    "device": "auto",
    "precision": "float32",
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


def resolve_implementation_plan(
    plan: Any,
    *,
    label: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Resolve recognized model tasks without rejecting unknown modalities."""
    candidate = dict(plan) if isinstance(plan, dict) else {}
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
    if not task_text.strip():
        task_text = str(description or "").lower()

    if _is_asr_task(task_text):
        return _merge_runtime_preferences(FASTER_WHISPER_PLAN, candidate)
    if _is_sentiment_task(task_text):
        return _merge_runtime_preferences(ROBERTA_SENTIMENT_PLAN, candidate)
    return candidate


def _is_asr_task(text: str) -> bool:
    return any(
        token in text
        for token in (
            "speech-to-text",
            "speech to text",
            "transcri",
            "automatic-speech-recognition",
            "whisper",
        )
    )


def _is_sentiment_task(text: str) -> bool:
    return "sentiment" in text or "twitter-roberta-base-sentiment" in text


def _merge_runtime_preferences(
    trusted: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    resolved = {key: _copy_value(value) for key, value in trusted.items()}
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
